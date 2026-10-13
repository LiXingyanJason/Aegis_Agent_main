"""真实主决策子图：按需卡片读取、交接、澄清及局部预算。"""
import json
from uuid import UUID

import pytest

from aegis_agent_worker.agent.specialists.main_agent import MainAgent
from aegis_agent_worker.agent.workflows.main.decision_workflow import MainDecisionWorkflow
from aegis_agent_worker.agent.contracts.context import MainContextReadRequest
from aegis_agent_worker.agent.contracts.decision import ConversationCapability
from aegis_agent_worker.tool.contracts import ToolError


CARD = str(UUID(int=90))
QUERY = str(UUID(int=91))
CAPS = (ConversationCapability(domain="mail", intent="query_mail", workflow="mail_query", description="查询"),)
READ = {"action": "read_context", "tool_name": "conversation.cards.get", "arguments": {"selector": "latest"}}


class Client:
    provider_name = model_name = "test"
    def __init__(self, outputs):
        self.outputs, self.calls = iter(outputs), []
    async def complete(self, messages): raise AssertionError("应走JSON mode")
    async def complete_json(self, messages):
        self.calls.append(messages)
        return json.dumps(next(self.outputs))


class Audit:
    def __init__(self): self.events = []
    async def load_references(self, run): raise AssertionError("不能预取邮箱引用")
    async def record_decision(self, run, **kwargs): self.events.append(kwargs)


class Tools:
    def __init__(self, fail=False): self.calls, self.fail = [], fail
    async def execute(self, run, request):
        self.calls.append(request)
        if self.fail: raise ToolError("CARD_NOT_FOUND", "不存在或无权访问")
        if request.tool_name == "conversation.cards.list":
            return {"cards": [{"card_id": CARD, "query_id": QUERY, "status": "valid"}]}
        return {"card_id": CARD, "query_id": QUERY, "status": "valid", "items": [
            {"position": 1, "email_id": str(UUID(int=9)), "source_version": "v1", "subject": "续约", "status": "valid"}]}
    async def resolve(self, run, reference, results):
        if reference.card_id != UUID(CARD) or reference.query_id != UUID(QUERY) or reference.position != 1:
            return None
        if not any(item.get("card_id") == CARD for item in results): return None
        return {"resource_type": "email", "id": str(UUID(int=9)), "source_version": "v1"}


def state(message="第一封给我摘要"):
    return dict(run_id=str(UUID(int=1)), tenant_id=str(UUID(int=2)), user_id=str(UUID(int=3)),
                conversation_id=str(UUID(int=4)), run_type="conversation", latest_user_message=message)


def final(position=1):
    return {"decision": "delegate", "domain": "mail", "intent": "query_mail", "instruction": "展示相应操作卡片",
            "card_reference": {"card_id": CARD, "query_id": QUERY, "position": position}}


async def invoke(outputs, tools=None, message="第一封给我摘要"):
    client, audit, tools = Client(outputs), Audit(), tools or Tools()
    graph = MainDecisionWorkflow(MainAgent(client), audit, CAPS, context_tools=tools).build()
    result = await graph.ainvoke(state(message))
    return result, client, audit, tools


@pytest.mark.asyncio
async def test_read_latest_then_resolve_reference_and_handoff_to_mail_card():
    """实际JSON模型请求经主图只读节点取得安全卡片，再核验位置构造领域交接。"""
    result, client, audit, tools = await invoke([READ, final()])
    assert result["workflow"] == "mail_query" and result["handoff"]["target"]["id"] == str(UUID(int=9))
    assert len(client.calls) == 2 and len(tools.calls) == 1
    assert "conversation.cards.get" in client.calls[0][0].content
    assert "续约" in client.calls[1][-1].content
    assert audit.events[0]["context_read_count"] == 1
    assert "续约" not in str(audit.events[0]["referenced_cards"])


@pytest.mark.asyncio
async def test_list_then_get_then_final_is_within_two_reads_and_three_decisions():
    """索引→具体卡片→最终决定严格有界，索引不能直接当对象引用。"""
    listing = {"action": "read_context", "tool_name": "conversation.cards.list", "arguments": {}}
    getting = {"action": "read_context", "tool_name": "conversation.cards.get", "arguments": {"card_id": CARD}}
    result, client, _, tools = await invoke([listing, getting, final()])
    assert result["workflow"] == "mail_query" and len(client.calls) == 3 and len(tools.calls) == 2


@pytest.mark.asyncio
async def test_plain_chat_does_not_read_any_card():
    """普通沟通可直接reply，不强制卡片工具或预取引用。"""
    result, client, _, tools = await invoke([{"decision": "reply"}], message="你好")
    assert result["workflow"] == "general_reply" and len(client.calls) == 1 and not tools.calls


@pytest.mark.asyncio
async def test_reply_after_read_only_passes_safe_context_to_reply_node():
    """主Agent只决策reply，安全事实通过reply_context交给正文生成，不自由生成业务结论。"""
    result, _, _, _ = await invoke([READ, {"decision": "reply"}], message="刚才展示的卡片是什么")
    assert result["workflow"] == "general_reply" and result["reply_context"][0]["card_id"] == CARD


@pytest.mark.asyncio
async def test_read_loop_cannot_exceed_local_budget():
    """连续要求读取不能形成无限ReAct，第三次请求澄清且不执行工具。"""
    result, client, audit, tools = await invoke([READ, READ, READ])
    assert result["workflow"] == "clarify" and not result["handoff"]
    assert len(client.calls) == 3 and len(tools.calls) == 2
    assert audit.events[0]["outcome"] == "context_budget_exceeded"


@pytest.mark.asyncio
async def test_read_failure_clarifies_without_default_mail_query():
    """不存在／越权读取不会变成收件箱列表，不再调用模型猜目标。"""
    result, client, audit, tools = await invoke([READ], Tools(fail=True))
    assert result["workflow"] == "clarify" and len(client.calls) == 1
    assert audit.events[0]["outcome"] == "context_read_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("outputs", [[final()], [READ, final(2)]])
async def test_unread_or_missing_position_does_not_delegate(outputs):
    """模型不能引用未读卡片或不存在位置，即使输出Schema合法也必须澄清。"""
    result, _, _, _ = await invoke(outputs)
    assert result["workflow"] == "clarify" and not result["handoff"]


@pytest.mark.asyncio
async def test_model_cannot_request_calendar_or_mail_business_tool_in_main_react():
    """卡片中即使诱导调用其他工具，主上下文请求Schema也只允许两种卡片工具。"""
    malicious = {"action": "read_context", "tool_name": "mail.messages.send", "arguments": {}}
    result, client, _, tools = await invoke([malicious, malicious])
    assert result["workflow"] == "clarify" and len(client.calls) == 2 and not tools.calls


@pytest.mark.asyncio
async def test_main_card_request_uses_existing_gateway_and_harness_budget():
    """真实Gateway和GuardedLLMClient共享run预算，卡片读取不绕过模型／工具计数。"""
    from test_harness_react_revision import Database, RUN
    from aegis_agent_worker.harness.executor import HarnessExecutor
    from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
    from aegis_agent_worker.tool.registry import ToolRegistry
    from aegis_agent_worker.tool.gateway import ToolGateway
    from aegis_agent_worker.tool.contracts import ToolDefinition, ToolResult, ToolInvocation
    from aegis_agent_worker.agent.contracts.context import CONTEXT_SCHEMAS
    from dataclasses import replace
    db = Database()
    run = replace(RUN, run_type="conversation", conversation_id=UUID(int=4))
    harness = HarnessExecutor(db)
    registered = ToolRegistry()
    class Handler:
        async def invoke(self, context, arguments):
            return ToolResult({"card_id": CARD, "query_id": QUERY, "status": "valid", "items": []}, {})
    registered.register(ToolDefinition(name="conversation.cards.get", version="1", risk_level="read",
        mcp_server="aegis-internal", description="卡片", argument_schema=CONTEXT_SCHEMAS["conversation.cards.get"]), Handler())
    gateway = ToolGateway(registered, harness=harness)
    class AuditedTools(Tools):
        async def execute(self, run, request):
            prepared = await gateway.prepare(None, tenant_id=run.tenant_id, user_id=run.user_id, run_id=run.id,
                invocation=ToolInvocation(request.tool_name, request.arguments))
            return (await gateway.invoke(prepared)).response_payload
    client = GuardedLLMClient(Client([READ, {"decision": "reply"}]), harness)
    graph = MainDecisionWorkflow(MainAgent(client), Audit(), CAPS, context_tools=AuditedTools()).build()
    async with harness.scope(run):
        await graph.ainvoke(state())
    assert db.budgets[run.id]["model_calls"] == 2 and db.budgets[run.id]["tool_calls"] == 1
