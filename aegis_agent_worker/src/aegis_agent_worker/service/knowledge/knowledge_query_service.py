"""知识检索的步骤、审计及事件编排；不承担 LLM 规划或证据回答。"""
from time import monotonic

from aegis_agent_worker.agent.contracts.knowledge import KnowledgeQueryPlan, KnowledgeSearchResult
from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.event.run_event import RunEventPublisher
from aegis_agent_worker.repository.run_repository import AgentRunRepository, ClaimedRun
from aegis_agent_worker.repository.tool_repository import ToolCallRepository
from aegis_agent_worker.tool.gateway import ToolGateway
from aegis_agent_worker.observability.telemetry import get_tracer
from aegis_agent_worker.tool.contracts import ToolError


class KnowledgeQueryService:
    def __init__(self, database: Database, runs: AgentRunRepository, tools: ToolGateway,
                 tool_calls: ToolCallRepository, events: RunEventPublisher | None = None) -> None:
        self._database, self._runs, self._tools = database, runs, tools
        self._tool_calls, self._events = tool_calls, events

    async def query(self, run: ClaimedRun, plan: KnowledgeQueryPlan) -> KnowledgeSearchResult:
        """只执行 knowledge.search；身份来自任务，调用在事务外，事件在提交后发布。"""
        plan = KnowledgeQueryPlan.model_validate(plan)
        invocation = plan.to_invocation()
        definition = self._tools.get_definition(invocation.tool_name)
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        summary = {"top_k": plan.top_k, "rewrite_count": len(plan.query_rewrites),
                   "category": plan.filters.category}
        started = monotonic()
        preparation_error = None
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                step_id = await self._runs.create_running_tool_step(session, run_id=run.id,
                    tenant_id=run.tenant_id, step_key="knowledge_search", label="正在检索知识资料")
                try:
                    prepared = await self._tools.prepare(session, tenant_id=run.tenant_id,
                        user_id=run.user_id, run_id=run.id, invocation=invocation)
                except ToolError as error:
                    preparation_error = error
                    prepared = None
                # 即使准备失败也保留尝试记录；正文只存审计表，不放入 SSE。
                call_id = await self._tool_calls.create_running(session, tenant_id=run.tenant_id,
                    run_id=run.id, step_id=step_id,
                    connection_id=prepared.context.connection_id if prepared else None,
                    tool_name=definition.name, risk_level=definition.risk_level,
                    tool_version=definition.version, mcp_server=definition.mcp_server,
                    request_payload=invocation.arguments, input_summary=summary)
                if preparation_error is not None:
                    await self._fail(session, run, step_id, call_id, preparation_error, 0)
                else:
                    await self._event(session, run, "progress_updated", {
                        "step_id": str(step_id), "label": "正在检索知识资料", "status": "running"})
        if preparation_error is not None:
            raise preparation_error
        try:
            with get_tracer(__name__).start_as_current_span("knowledge.search") as span:
                span.set_attribute("aegis.run_id", str(run.id))
                span.set_attribute("aegis.tool.name", definition.name)
                raw = await self._tools.invoke(prepared)
            try:
                result = KnowledgeSearchResult.model_validate(raw.response_payload)
                if len(result.evidence) > plan.top_k:
                    raise ValueError("超过请求结果上限")
            except ValueError as error:
                raise ToolError("KNOWLEDGE_INVALID_EVIDENCE", "知识检索返回的证据无效") from error
        except ToolError as error:
            async with self._database.session() as session:
                async with tenant_transaction(session, context):
                    await self._fail(session, run, step_id, call_id, error,
                                     int((monotonic()-started)*1000))
            raise
        # 不直接信任 Handler 的任意摘要字段；预览只包含计数和稳定引用。
        output = {"evidence_count": len(result.evidence), "sources": [
            {"document_id": str(item.document_id), "version_id": str(item.version_id),
             "chunk_id": str(item.chunk_id)} for item in result.evidence]}
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                await self._tool_calls.succeed(session, tool_call_id=call_id, tenant_id=run.tenant_id,
                    response_payload=result.model_dump(mode="json"), output_summary=output,
                    duration_ms=int((monotonic()-started)*1000))
                await self._runs.finish_step(session, step_id=step_id, run_id=run.id,
                    tenant_id=run.tenant_id, status="succeeded", detail="知识检索完成")
                await self._event(session, run, "tool_preview", {"tool_call_id": str(call_id),
                    "tool_name": definition.name, "risk_level": definition.risk_level,
                    "input_summary": summary, "output_summary": output, "status": "succeeded"})
                await self._event(session, run, "progress_updated", {"step_id": str(step_id),
                    "label": "正在检索知识资料", "status": "succeeded", "detail": "知识检索完成"})
        return result

    async def _fail(self, session, run, step_id, call_id, error, duration_ms):
        await self._tool_calls.fail(session, tool_call_id=call_id, tenant_id=run.tenant_id,
            error_code=error.code, error_message=error.message, duration_ms=duration_ms)
        await self._runs.finish_step(session, step_id=step_id, run_id=run.id,
            tenant_id=run.tenant_id, status="failed", detail=error.message)
        await self._event(session, run, "tool_preview", {"tool_call_id": str(call_id),
            "tool_name": "knowledge.search", "risk_level": "read", "status": "failed",
            "error_code": error.code, "error_message": error.message})
        await self._event(session, run, "progress_updated", {"step_id": str(step_id),
            "label": "知识检索不可用", "status": "failed", "detail": error.message})

    async def _event(self, session, run, event_type, payload):
        if self._events is not None:
            event = await self._runs.append_event(session, run_id=run.id, tenant_id=run.tenant_id,
                                                  event_type=event_type, payload=payload)
            self._events.publish_after_commit(event)
