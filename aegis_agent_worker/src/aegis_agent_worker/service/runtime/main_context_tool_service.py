"""主决策上下文工具的事务与审计编排，执行仍走统一Gateway/Harness。"""
from time import monotonic
from uuid import uuid4

from aegis_agent_worker.config.database import TenantContext, tenant_transaction
from aegis_agent_worker.repository.tool_repository import ToolCallRepository
from aegis_agent_worker.tool.contracts import ToolInvocation


class MainContextToolService:
    def __init__(self, database, runs, tools, cards, events=None, calls=None):
        self._database, self._runs, self._tools = database, runs, tools
        self._cards, self._events = cards, events
        self._calls = calls or ToolCallRepository()

    async def execute(self, run, request):
        context = TenantContext(run.tenant_id, run.user_id)
        started = monotonic()
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                prepared = await self._tools.prepare(session, tenant_id=run.tenant_id, user_id=run.user_id,
                    run_id=run.id, invocation=ToolInvocation(request.tool_name, request.arguments))
                definition = prepared.registered.definition
                step = await self._runs.create_running_tool_step(session, run_id=run.id, tenant_id=run.tenant_id,
                    step_key=f"context_read_{uuid4().hex}", label="读取卡片上下文")
                call = await self._calls.create_running(session, tenant_id=run.tenant_id, run_id=run.id,
                    step_id=step, connection_id=None, tool_name=definition.name, risk_level="read",
                    tool_version=definition.version, mcp_server=definition.mcp_server,
                    request_payload=request.arguments, input_summary={"tool_name": definition.name})
        try:
            result = await self._tools.invoke(prepared)
        except Exception as error:
            async with self._database.session() as session:
                async with tenant_transaction(session, context):
                    await self._calls.fail(session, tool_call_id=call, tenant_id=run.tenant_id,
                        error_code=getattr(error, "code", "CONTEXT_READ_FAILED"), error_message="卡片读取失败",
                        duration_ms=round((monotonic()-started)*1000))
                    await self._runs.finish_step(session, step_id=step, run_id=run.id, tenant_id=run.tenant_id,
                                                 status="failed", detail="卡片读取失败")
            raise
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                await self._calls.succeed(session, tool_call_id=call, tenant_id=run.tenant_id,
                    response_payload=result.output_summary, output_summary=result.output_summary,
                    duration_ms=round((monotonic()-started)*1000))
                await self._runs.finish_step(session, step_id=step, run_id=run.id, tenant_id=run.tenant_id,
                                             status="succeeded", detail="已读取卡片上下文")
                event = await self._runs.append_event(session, run_id=run.id, tenant_id=run.tenant_id,
                    event_type="tool_preview", payload={"tool_call_id": str(call), "tool_name": request.tool_name,
                    "risk_level": "read", "status": "succeeded", "output_summary": result.output_summary})
                if self._events:
                    self._events.publish_after_commit(event)
        return result.response_payload

    async def resolve(self, run, reference, read_results):
        return await self._cards.resolve(run, reference, read_results)
