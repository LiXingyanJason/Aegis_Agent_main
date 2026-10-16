"""领域服务隔离、事务边界及安全预览的替身测试。"""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from aegis_agent_worker.agent.contracts.knowledge import KnowledgeQueryPlan
from aegis_agent_worker.service.calendar.calendar_tool_execution_service import CalendarToolExecutionService
from aegis_agent_worker.service.knowledge.knowledge_query_service import KnowledgeQueryService
from aegis_agent_worker.tool.contracts import ToolError, ToolInvocation
from test_knowledge_workflow import evidence
from test_harness_layers import RUN


class Database:
    active = False

    @asynccontextmanager
    async def session(self):
        database = self
        class Session:
            @asynccontextmanager
            async def begin(self):
                database.active = True
                try:
                    yield
                finally:
                    database.active = False
            async def execute(self, *args):
                pass
        yield Session()


def setup(mode="success"):
    database = Database()
    runs = SimpleNamespace(create_running_tool_step=AsyncMock(return_value=UUID(int=10)),
        finish_step=AsyncMock(), append_event=AsyncMock())
    calls = SimpleNamespace(create_running=AsyncMock(return_value=UUID(int=11)),
                            succeed=AsyncMock(), fail=AsyncMock())
    definition = SimpleNamespace(name="knowledge.search", risk_level="read", version="1", mcp_server="internal")
    tools = SimpleNamespace(get_definition=Mock(return_value=definition),
        prepare=AsyncMock(return_value=SimpleNamespace(context=SimpleNamespace(connection_id=None))))
    async def invoke(prepared):
        assert not database.active, "工具执行不能占用数据库事务"
        if mode == "invoke_error":
            raise ToolError("KNOWLEDGE_NOT_CONFIGURED", "尚未配置")
        return SimpleNamespace(response_payload={"evidence": [evidence().model_dump(mode="json")]} if mode != "invalid" else {"tenant_id": "bad"},
                               output_summary={"query": "不可进入SSE", "excerpt": "不可进入SSE"})
    tools.invoke = AsyncMock(side_effect=invoke)
    if mode == "prepare_error":
        tools.prepare.side_effect = ToolError("TOOL_INVALID_ARGUMENTS", "参数无效")
    events = SimpleNamespace(publish_after_commit=Mock())
    return KnowledgeQueryService(database, runs, tools, calls, events), runs, tools, calls


@pytest.mark.asyncio
async def test_success_is_structured_and_preview_contains_only_safe_references():
    service, runs, tools, calls = setup()
    result = await service.query(RUN, KnowledgeQueryPlan(query="续约政策"))
    assert result.evidence == [evidence()]
    calls.succeed.assert_awaited_once()
    preview = next(call.kwargs["payload"] for call in runs.append_event.await_args_list
                   if call.kwargs["event_type"] == "tool_preview")
    assert preview["output_summary"]["evidence_count"] == 1
    assert "excerpt" not in str(preview) and "续约政策" not in str(preview)
    assert tools.prepare.await_args.kwargs["tenant_id"] == RUN.tenant_id


@pytest.mark.asyncio
async def test_retrieval_diagnostics_preserved_in_tool_audit_and_preview():
    service,runs,tools,calls=setup()
    diagnostics={'policy_version':'hybrid-v1','mode':'lexical_only','degraded':True,
        'reason':'vector_unavailable','rankings':[]}
    tools.invoke=AsyncMock(return_value=SimpleNamespace(response_payload={
        'evidence':[evidence().model_dump(mode='json')],'retrieval':diagnostics},output_summary={}))
    result=await service.query(RUN,KnowledgeQueryPlan(query='续约政策'))
    assert result.retrieval.degraded
    assert calls.succeed.await_args.kwargs['response_payload']['retrieval']==diagnostics
    preview=next(call.kwargs['payload'] for call in runs.append_event.await_args_list if call.kwargs['event_type']=='tool_preview')
    assert preview['output_summary']['retrieval']==diagnostics
    assert '续约政策' not in str(preview) and 'excerpt' not in str(preview)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,code", [("prepare_error", "TOOL_INVALID_ARGUMENTS"),
    ("invoke_error", "KNOWLEDGE_NOT_CONFIGURED"), ("invalid", "KNOWLEDGE_INVALID_EVIDENCE")])
async def test_failures_are_audited_without_success_or_calendar_events(mode, code):
    service, runs, tools, calls = setup(mode)
    with pytest.raises(ToolError) as error:
        await service.query(RUN, KnowledgeQueryPlan(query="政策"))
    assert error.value.code == code
    calls.fail.assert_awaited_once()
    calls.succeed.assert_not_awaited()
    assert runs.finish_step.await_args.kwargs["status"] == "failed"
    assert all(call.kwargs["event_type"] != "connection_required" for call in runs.append_event.await_args_list)
    if mode == "prepare_error":
        tools.invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_plan_is_rejected_before_database_or_gateway():
    service, runs, tools, calls = setup()
    with pytest.raises(ValueError):
        await service.query(RUN, {"query": "政策", "tenant_id": "other"})
    tools.get_definition.assert_not_called()
    runs.create_running_tool_step.assert_not_awaited()


@pytest.mark.asyncio
async def test_calendar_service_rejects_knowledge_before_gateway_or_database():
    tools = SimpleNamespace(get_definition=Mock())
    service = CalendarToolExecutionService(None, None, tools, None)
    with pytest.raises(ToolError) as error:
        await service.execute_read_tool(RUN, None, ToolInvocation("knowledge.search", {"query": "政策"}))
    assert error.value.code == "TOOL_NOT_ALLOWED"
    tools.get_definition.assert_not_called()
