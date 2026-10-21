"""邮件查询业务执行；LLM 规划在 MailAgent／Workflow。"""

from time import monotonic
from uuid import uuid4
from pydantic import ValidationError

from aegis_agent_worker.agent.contracts.mail import MailQueryPlan, MailListMessage
from aegis_agent_worker.config.database import TenantContext, tenant_transaction
from aegis_agent_worker.repository.mail_query_repository import MailQueryRepository
from aegis_agent_worker.repository.tool_repository import ToolCallRepository
from aegis_agent_worker.tool.contracts import ToolInvocation, ToolError


class MailQueryService:
    def __init__(self, database, runs, tools, events=None, repository=None, tool_calls=None):
        self._database, self._runs, self._tools, self._events = database, runs, tools, events
        self._repository = repository or MailQueryRepository()
        self._tool_calls = tool_calls or ToolCallRepository()

    async def _event(self, session, run, event_type, payload):
        event = await self._runs.append_event(session, run_id=run.id, tenant_id=run.tenant_id,
                                             event_type=event_type, payload=payload)
        if self._events:
            self._events.publish_after_commit(event)

    async def query(self, run, plan: MailQueryPlan, target=None):
        """经 Gateway 读取元数据、同步快照、确定性筛选，不产生发送等外部副作用。"""
        if plan.action == "clarify":
            raise ValueError("澄清计划不能查询工具")
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        started = monotonic()
        definition = self._tools.get_definition("mail.messages.list")
        tool_id = None
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                step_id = await self._runs.create_running_tool_step(
                    session, run_id=run.id, tenant_id=run.tenant_id,
                    step_key=f"mail_query_{uuid4().hex}", label="正在查询邮件")
                tool_id = await self._tool_calls.create_running(
                    session, tenant_id=run.tenant_id, run_id=run.id, step_id=step_id, connection_id=None,
                    tool_name=definition.name, risk_level=definition.risk_level, tool_version=definition.version,
                    mcp_server=definition.mcp_server, request_payload={}, input_summary={"action": plan.action})
                await self._event(session, run, "progress_updated", {
                    "step_id": str(step_id), "label": "正在查询邮件", "status": "running"})
        try:
            # 网络调用前完成连接授权事务，避免等待 MCP 时占用连接。
            async with self._database.session() as session:
                async with tenant_transaction(session, context):
                    prepared = await self._tools.prepare(
                        session, tenant_id=run.tenant_id, user_id=run.user_id, run_id=run.id,
                        invocation=ToolInvocation("mail.messages.list", {}))
                    await self._tool_calls.bind_connection(session, tool_call_id=tool_id,
                                                           tenant_id=run.tenant_id,
                                                           connection_id=prepared.context.connection_id)
            result = await self._tools.invoke(prepared)
            raw = result.response_payload
            if not isinstance(raw, dict) or not isinstance(raw.get("messages"), list):
                raise ToolError("MAIL_INVALID_RESPONSE", "邮件工具未返回合法列表")
            try:
                messages = [MailListMessage.model_validate(item) for item in raw["messages"]]
            except ValidationError as error:
                raise ToolError("MAIL_INVALID_RESPONSE", "邮件元数据格式无效") from error
            ids = [item.provider_message_id for item in messages]
            if len(set(ids)) != len(ids):
                raise ToolError("MAIL_INVALID_RESPONSE", "邮件列表含重复标识")
            messages.sort(key=lambda item: (item.received_at, item.provider_message_id), reverse=True)
            source = raw.get("source")
            if not isinstance(source, str) or not source or len(source) > 100:
                raise ToolError("MAIL_INVALID_RESPONSE", "邮件来源格式无效")
            async with self._database.session() as session:
                async with tenant_transaction(session, context):
                    rows = await self._repository.upsert(session, run, prepared.context.connection_id, messages)
                    matched = ([row for row in rows if row["id"] == target.id] if target is not None
                               else [row for message, row in zip(messages, rows) if plan.matches(message)])
                    # 审计仅保存已经通过字段白名单的元数据，不保存额外外部字段或正文。
                    await self._tool_calls.succeed(
                        session, tool_call_id=tool_id, tenant_id=run.tenant_id,
                        response_payload={"source": source, "messages": [item.model_dump(mode="json") for item in messages]},
                        output_summary={"source": source, "mail_count": len(messages), "matched_count": len(matched)},
                        duration_ms=round((monotonic() - started) * 1000))
                    await self._runs.finish_step(session, step_id=step_id, run_id=run.id,
                                                tenant_id=run.tenant_id, status="succeeded", detail="邮件查询完成")
                    await self._event(session, run, "tool_preview", {
                        "tool_call_id": str(tool_id), "tool_name": "mail.messages.list", "risk_level": "read",
                        "status": "succeeded", "output_summary": {
                            "source": source, "mail_count": len(messages), "matched_count": len(matched),
                        }})
                    await self._event(session, run, "progress_updated", {
                        "step_id": str(step_id), "label": "邮件查询完成", "status": "succeeded"})
            return matched, source
        except ToolError as error:
            async with self._database.session() as session:
                async with tenant_transaction(session, context):
                    await self._tool_calls.fail(session, tool_call_id=tool_id, tenant_id=run.tenant_id,
                                                error_code=error.code, error_message=error.message,
                                                duration_ms=round((monotonic() - started) * 1000))
                    await self._runs.finish_step(session, step_id=step_id, run_id=run.id,
                                                tenant_id=run.tenant_id, status="failed", detail=error.message)
                    await self._event(session, run, "progress_updated", {
                        "step_id": str(step_id), "label": "邮件查询失败", "status": "failed",
                        "detail": error.message})
                    await self._event(session, run, "tool_preview", {
                        "tool_call_id": str(tool_id), "tool_name": "mail.messages.list", "risk_level": "read",
                        "status": "failed", "error_code": error.code, "error_message": error.message})
                    if error.code == "MAIL_CONNECTION_REQUIRED":
                        await self._event(session, run, "connection_required", {
                            "provider": "mail", "required_scopes": ["mail.read"], "message": error.message})
            raise
