"""仅记录代码产生的协作进度，不保存用户目标、模型响应或专家正文。"""
from uuid import UUID, uuid5
import re
from sqlalchemy import text
from aegis_agent_worker.event.cooperation_context import association as make_association, validate_association

from aegis_agent_worker.config.database import TenantContext, tenant_transaction


LABELS = {"started":"开始协作","coordinating":"协调下一步","delegated":"已委派专家",
    "expert_started":"专家开始处理","expert_completed":"专家处理返回","waiting_input":"等待补充信息",
    "resumed":"恢复专家处理","draft_ready":"会议草稿已就绪","finished":"团队生成阶段结束",
    "awaiting_confirmation":"等待用户核对会议","unavailable":"协作暂无法完成","failed":"协作执行受阻"}
PHASES = frozenset(LABELS)


class TeamEventService:
    def __init__(self, database, runs, events=None):
        self._database, self._runs, self._events = database, runs, events

    async def record(self, run, *, phase, round_no, policy_version, delegation=None, error_code=None,
                     association=None, question_id=None, result=None, session=None):
        if phase not in PHASES or type(round_no) is not int or round_no < 0:
            raise ValueError("协作事件参数无效")
        payload = {"version": 1, "phase": phase, "round": round_no,
                   "execution_policy_version": policy_version,"label":LABELS[phase]}
        correlation=association or make_association(run.id,round=round_no)
        if error_code is not None:
            if not isinstance(error_code,str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}",error_code):
                raise ValueError("协作错误码无效")
            payload["error_code"] = error_code
        if delegation is not None:
            payload.update(delegation_id=str(UUID(str(delegation.delegation_id))),
                           agent_id=delegation.target, sequence=delegation.sequence)
            correlation=make_association(run.id,delegation.target,round=round_no,
                delegation_id=delegation.delegation_id,sequence=delegation.sequence)
        payload["cooperation"]=validate_association(run.id,correlation)
        if question_id is not None: payload["question_id"]=str(UUID(str(question_id)))
        if result is not None:
            # 只接受已经过注册表核验的结构化产物；不复制自由文本summary。
            payload.update(result_status=result.status,references=[ref.model_dump(mode="json") for ref in result.references])
            if result.error_code: payload["error_code"]=result.error_code
            payload["unresolved_fields"]=list(result.unresolved_fields)
            if result.output_stage is not None:
                payload.update(output_stage=result.output_stage, repair_count=result.repair_count,
                    validation_issues=[item.model_dump() for item in result.validation_issues])
            if result.draft: payload["draft"]=result.draft.model_dump(mode="json")
        key=f"{phase}:{round_no}:{payload['cooperation'].get('delegation_id','')}:{question_id or ''}"
        payload["cooperation_event_id"]=str(uuid5(run.id,key))
        async def persist(session):
            await session.execute(text("SELECT id FROM agent_runs WHERE id=:run AND tenant_id=:tenant AND user_id=:user FOR UPDATE"),
                dict(run=run.id,tenant=run.tenant_id,user=run.user_id))
            previous=await session.execute(text("SELECT event_no FROM run_events WHERE run_id=:run AND tenant_id=:tenant "
                "AND event_type='cooperation_progress' AND payload->>'cooperation_event_id'=:key LIMIT 1"),
                dict(run=run.id,tenant=run.tenant_id,key=payload["cooperation_event_id"]))
            if previous.mappings().first() is not None: return
            event=await self._runs.append_event(session,run_id=run.id,tenant_id=run.tenant_id,
                event_type="cooperation_progress",payload=payload)
            if self._events is not None: self._events.publish_after_commit(event)
        if session is not None:
            await persist(session)
            return
        async with self._database.session() as session:
            async with tenant_transaction(session, TenantContext(run.tenant_id, run.user_id)):
                await persist(session)
