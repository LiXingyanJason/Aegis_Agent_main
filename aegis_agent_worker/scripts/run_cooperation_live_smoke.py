"""显式授权的真实模型/DB/Redis/Mock MCP协作冒烟；不用于真实日历。"""
import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4
from urllib.parse import urlparse

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"Aegis_backend"))
from app.repository.conversation_repository import ConversationRepository
from app.repository.agent_input_repository import AgentInputRepository
from app.repository.approval_repository import ApprovalRepository
from app.param.agent_input_param import InputAnswerParam
from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.config.settings import get_settings
from aegis_agent_worker.config.redis import create_redis_client
from aegis_agent_worker.observability.trace_context import serialize_run_message
from aegis_agent_worker.worker import agent_worker


ATTENDEES = {"linyue@xinghe.example.com", "zhouchen@xinghe.example.com"}
GOAL = "请按续约邮件中的时间要求查询日历并安排会议草稿"
ANSWER = ("请选择协同测试A｜星河服务续约会议。按邮件明确的北京时间2026-10-26 09:00至12:00，"
          "安排50分钟，参会人为linyue@xinghe.example.com和zhouchen@xinghe.example.com。"
          "先按我的空闲时间生成草稿，其他参会人的空闲尚未核验，不能声称他们已确认。")


def emit(kind, **fields):
    print(json.dumps(dict(kind=kind, **fields), ensure_ascii=False, default=str), flush=True)


async def main(args):
    settings = get_settings()
    for endpoint, port in ((settings.calendar_mcp_url, 9001), (settings.email_mcp_url, 9002)):
        parsed = urlparse(str(endpoint))
        if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != port:
            raise RuntimeError("仅允许本机9001/9002的已核验Mock MCP服务")
    if not settings.cooperation_enabled:
        raise RuntimeError("请先启用COOPERATION_ENABLED，不由脚本扩增能力")
    queue = "aegis:smoke:"+str(uuid4())
    isolated = settings.model_copy(update={"agent_queue_name": queue, "otel_enabled": False,
                                           "database_echo": False})
    agent_worker.get_settings = lambda: isolated
    db, redis = Database(isolated), create_redis_client(isolated)
    worker = None
    try:
        async with db.session() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            owner = (await session.execute(text("SELECT tenant_id,user_id FROM agent_runs WHERE id=:id"),
                                            dict(id=UUID(args.source_run_id)))).mappings().one()
            user = SimpleNamespace(id=owner["user_id"], tenant_id=owner["tenant_id"])
            await session.rollback()
        identity = TenantContext(user.tenant_id, user.id)
        async with db.session() as session:
            async with tenant_transaction(session, identity):
                repository = ConversationRepository()
                conversation = await repository.create(session, tenant_id=user.tenant_id, user_id=user.id,
                    title="协作真实冒烟A-"+datetime.now().isoformat(timespec="seconds"))
                sent = await repository.create_message_and_run(session, conversation_id=conversation.id,
                    tenant_id=user.tenant_id, user_id=user.id, content=GOAL, client_message_id=str(uuid4()))
        run_id = sent.run_id
        emit("created", run_id=run_id, conversation_id=conversation.id)
        worker = asyncio.create_task(agent_worker.run_worker())
        await redis.rpush(queue, serialize_run_message(run_id))
        handled = set()
        approved = False
        previous = None
        deadline = asyncio.get_running_loop().time()+args.timeout
        while asyncio.get_running_loop().time() < deadline:
            if worker.done():
                await worker
                raise RuntimeError("隔离Worker意外结束")
            async with db.session() as session:
                async with tenant_transaction(session, identity):
                    row = (await session.execute(text("SELECT status,error_code FROM agent_runs "
                        "WHERE id=:run AND tenant_id=:tenant AND user_id=:user"),
                        dict(run=run_id, tenant=user.tenant_id, user=user.id))).mappings().one()
                    status = row["status"]
                    if status != previous:
                        emit("status", status=status, error_code=row["error_code"])
                        previous = status
                    if status in {"completed", "failed", "cancelled"}:
                        events = (await session.execute(text("SELECT event_no,payload->>'phase' AS phase,"
                            "payload->>'error_code' AS error_code,payload->>'output_stage' AS output_stage,"
                            "payload->'validation_issues' AS validation_issues FROM run_events "
                            "WHERE run_id=:run AND tenant_id=:tenant AND event_type='cooperation_progress' ORDER BY event_no"),
                            dict(run=run_id, tenant=user.tenant_id))).mappings().all()
                        emit("finished", run_id=run_id, status=status, approved=approved,
                             cooperation=[dict(item) for item in events])
                        writes = (await session.execute(text("SELECT count(*) AS calls,"
                            "count(*) FILTER(WHERE status='succeeded') AS succeeded FROM tool_calls "
                            "WHERE run_id=:run AND tenant_id=:tenant AND tool_name='calendar.create_event'"),
                            dict(run=run_id, tenant=user.tenant_id))).mappings().one()
                        executed = (await session.execute(text("SELECT count(*) FROM approval_items WHERE run_id=:run "
                            "AND tenant_id=:tenant AND user_id=:user AND status='executed'"),
                            dict(run=run_id, tenant=user.tenant_id, user=user.id))).scalar_one()
                        emit("write_verification", **dict(writes), executed_approvals=executed)
                        return status == "completed" and approved and writes["calls"] == writes["succeeded"] == executed == 1
                    if status == "waiting_input":
                        requests, _ = await AgentInputRepository().list_inputs(session, user, run_id)
                        for question in requests:
                            if question["status"] != "pending" or not question["ready"] or question["question_id"] in handled:
                                continue
                            kwargs = dict(expected_version=question["version"], client_response_id=str(uuid4()))
                            if question["input_type"] == "single_choice":
                                options = [item for item in question["options"] if "协同测试A" in item["label"]]
                                if len(options) != 1:
                                    raise RuntimeError("测试A选项非唯一，拒绝代选")
                                kwargs["selected_option_id"] = options[0]["id"]
                            else:
                                if len(handled) >= 4:
                                    raise RuntimeError("补问超出冒烟测试上限")
                                kwargs["response"] = ANSWER
                            result = await AgentInputRepository().answer(session, user, question["question_id"],
                                                                          InputAnswerParam(**kwargs))
                            if not isinstance(result, dict) or not result["execution_scheduled"]:
                                raise RuntimeError("补问答案被业务校验拒绝")
                            handled.add(question["question_id"])
                            emit("answered", input_type=question["input_type"], question_id=question["question_id"])
                            resume = dict(resume_input_id=question["question_id"])
                            break
                        else:
                            resume = None
                    elif status == "waiting_confirmation" and not approved:
                        approvals = await ApprovalRepository().list_for_user(session, tenant_id=user.tenant_id,
                            user_id=user.id, run_id=run_id, status="pending")
                        if len(approvals) != 1:
                            raise RuntimeError("确认项非唯一，拒绝批准")
                        item = approvals[0]
                        sources = (await session.execute(text("SELECT response_payload->>'source' AS source "
                            "FROM tool_calls WHERE run_id=:run AND tenant_id=:tenant AND status='succeeded' "
                            "AND tool_name IN ('calendar.list_events','calendar.find_free_time')"),
                            dict(run=run_id, tenant=user.tenant_id))).scalars().all()
                        if not sources or any(source != "mock_calendar" for source in sources):
                            raise RuntimeError("日历只读响应并非已知Mock来源，拒绝批准")
                        snapshot = item["preview_snapshot"]
                        if isinstance(snapshot, str): snapshot = json.loads(snapshot)
                        start, end = datetime.fromisoformat(snapshot["start_at"]), datetime.fromisoformat(snapshot["end_at"])
                        lower, upper = datetime.fromisoformat("2026-10-26T09:00:00+08:00"), datetime.fromisoformat("2026-10-26T12:00:00+08:00")
                        if (item["action"] != "calendar.create_event" or set(snapshot["attendees"]) != ATTENDEES
                                or not lower <= start < end <= upper or end-start != timedelta(minutes=50)
                                or snapshot["title"] != "星河服务续约方案确认"):
                            raise RuntimeError("草稿与测试A约束不一致，拒绝批准")
                        decision = await ApprovalRepository().decide(session, approval_item_id=item["id"],
                            tenant_id=user.tenant_id, user_id=user.id, decision="approved",
                            reason="用户授权的Mock日历端到端测试，已核对测试A两位参会人及时间")
                        if decision.outcome != "decided": raise RuntimeError("审批未通过业务守卫")
                        approved = True
                        emit("approved", approval_item_id=item["id"], preview=snapshot)
                        resume = dict(resume_decision="approved")
                    else:
                        resume = None
            # API等价边界：只有DB提交后才投递恢复消息。
            if resume: await redis.rpush(queue, serialize_run_message(run_id, **resume))
            await asyncio.sleep(1)
        raise TimeoutError("冒烟超时；保留任务供检查，不自动重放或清理")
    finally:
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError): await worker
        await redis.aclose()
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", required=True, type=str)
    parser.add_argument("--allow-model-calls", required=True, action="store_true")
    parser.add_argument("--approve-mock-calendar", required=True, action="store_true")
    parser.add_argument("--timeout", type=int, default=480)
    args = parser.parse_args()
    if sys.platform == "win32": asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(0 if asyncio.run(main(args)) else 1)
