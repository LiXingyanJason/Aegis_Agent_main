"""主决策 Schema、交接核验、根图分流与审批恢复回归。"""

from uuid import UUID

import pytest
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver

from aegis_agent_worker.agent.contracts.decision import MainDecision, ConversationCapability
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.graph.task_graph import TaskGraph, TaskGraphDependencies
from aegis_agent_worker.agent.specialists.main_agent import MainAgent
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.service.runtime.decision_context_service import DecisionContextService
from aegis_agent_worker.agent.workflows.main.decision_workflow import MainDecisionWorkflow

EMAIL_ID = UUID(int=9)
RUN = ClaimedRun(id=UUID(int=1), tenant_id=UUID(int=2), user_id=UUID(int=3), conversation_id=UUID(int=4))


@pytest.mark.parametrize("payload", [
    '{"decision":"delegate","domain":"calendar","intent":"extract_mail","instruction":"摘要"}',
    '{"decision":"delegate","domain":"mail","intent":"extract_mail","instruction":"摘要"}',
    '{"decision":"delegate","domain":"calendar","intent":"delete_event","instruction":"删除"}',
    '{"decision":"reply","target":{"resource_type":"email","id":"00000000-0000-0000-0000-000000000009"}}',
    '{"decision":"clarify"}',
    '{"decision":"reply","approved":true}',
])
def test_decision_rejects_invalid_or_executable_fields(payload):
    """未知意图、领域错配、缺失目标和伪造批准字段不能成为执行计划。"""
    with pytest.raises(ValueError):
        MainDecision.from_text(payload)


class FakeMain:
    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    async def decide(self, **kwargs):
        self.calls += 1
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


class FakeRepository:
    async def list_references(self, session, run):
        return [{"resource_type": "email", "id": str(EMAIL_ID), "source_version": "v1", "label": "续约", "position": 1}]

    async def resolve_reference(self, session, run, reference):
        if reference.id == EMAIL_ID and reference.source_version == "v1":
            return {"resource_type": "email", "id": str(EMAIL_ID), "source_version": "v1"}
        return None


class Service(DecisionContextService):
    __test__ = False

    async def _read(self, run, method, *args):
        return await method(None, run, *args)

    async def record_decision(self, run, **kwargs):
        outcome = kwargs["outcome"]
        self.last_audit = outcome


def workflow_state(**extra):
    """子图读取可信 ID 字段，内部候选状态与根图输出协议分别验证。"""
    return dict(run_id=str(RUN.id), tenant_id=str(RUN.tenant_id), user_id=str(RUN.user_id),
                conversation_id=str(RUN.conversation_id), run_type="conversation", **extra)


async def decide(main, service, capabilities, **state):
    """通过实际 LangGraph 子图执行决策，不把推理塞回 Service。"""
    return await MainDecisionWorkflow(main, service, capabilities).build().ainvoke(workflow_state(**state))


CAPABILITIES = (
    ConversationCapability(domain="calendar", intent="list_events", workflow="calendar_read", description="读取"),
    ConversationCapability(domain="mail", intent="query_mail", workflow="mail_query",
                           description="邮件卡片"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("target_id,source_version,expected", [
    (EMAIL_ID, "v1", "mail_query"),
    (EMAIL_ID, "v0", "clarify"),
    (UUID(int=99), "v1", "clarify"),
])
async def test_mail_target_is_offered_owned_and_current(target_id, source_version, expected):
    """模型只能交接服务端提供且仍有效的邮件引用，过期及伪造引用转为澄清。"""
    decision = MainDecision.model_validate({
        "decision": "delegate", "domain": "mail", "intent": "query_mail", "instruction": "展示对应卡片",
        "target": {"resource_type": "email", "id": target_id, "source_version": source_version},
    })
    service = Service(None, None, repository=FakeRepository())
    result = await decide(FakeMain(decision), service, CAPABILITIES, latest_user_message="摘要")
    assert result["workflow"] == expected
    assert bool(result["handoff"]) == (expected != "clarify")


@pytest.mark.asyncio
async def test_unregistered_capability_and_invalid_json_clarify():
    """Schema 合法但未注册的能力及无法解析的输出不能触发工具。"""
    for decision in [
        MainDecision(decision="delegate", domain="calendar", intent="arrange_meeting", instruction="安排会议"),
        ValueError("非法 JSON"),
    ]:
        result = await decide(FakeMain(decision), Service(None, None), CAPABILITIES[:1])
        assert result["workflow"] == "clarify"
        assert result["handoff"] == {}


@pytest.mark.asyncio
async def test_ambiguous_target_returns_question_and_model_failure_is_audited():
    """澄清不形成交接，模型不可用被记录且不静默执行默认领域。"""
    from aegis_agent_worker.llm.client import LLMError
    service = Service(None, None, repository=FakeRepository())
    result = await decide(FakeMain(MainDecision(
        decision="clarify", clarification_question="请明确要摘要哪封邮件？")), service, CAPABILITIES)
    assert result["workflow"] == "clarify" and not result["handoff"]
    failing = Service(None, None)
    with pytest.raises(LLMError):
        await decide(FakeMain(LLMError("不可用")), failing, CAPABILITIES[:1])
    assert failing.last_audit == "model_failed"


class Factory:
    def __init__(self):
        self.handoffs = []

    def register(self, registry):
        async def confirm(state):
            self.handoffs.append(state.get("handoff"))
            decision = interrupt({"status": "waiting_confirmation"})
            return {"assistant_content": "已批准" if decision == "approved" else "已拒绝"}
        calendar = StateGraph(TaskGraphState)
        calendar.add_node("confirm", confirm)
        calendar.add_edge(START, "confirm")
        calendar.add_edge("confirm", END)
        registry.register("calendar_confirmation", calendar.compile())
        registry.register_capability(ConversationCapability(
            domain="calendar", intent="arrange_meeting", workflow="calendar_confirmation", description="安排会议"))
        mail = StateGraph(TaskGraphState)
        mail.add_node("mail", lambda state: {"mail_workflow_completed": True})
        mail.add_edge(START, "mail")
        mail.add_edge("mail", END)
        registry.register("mail_extraction", mail.compile())


class Context:
    async def load_history(self, *args):
        return [{"role": "user", "content": "想约一个半小时的总结会"}]


@pytest.mark.asyncio
async def test_root_delegates_handoff_and_resume_skips_main_agent():
    """普通对话经主 Agent 交接；同一 thread 的审批恢复不再次分派。"""
    main = FakeMain(MainDecision(
        decision="delegate", domain="calendar", intent="arrange_meeting", instruction="安排30分钟总结会议"))
    service = Service(None, None)
    factory = Factory()
    graph = TaskGraph(TaskGraphDependencies(
        context_service=Context(), lifecycle=None, llm_client=None, system_prompt="",
        workflow_factories=(factory,),
        main_agent=main, decision_context_service=service,
    )).build(checkpointer=MemorySaver())
    state = dict(run_id=str(RUN.id), tenant_id=str(RUN.tenant_id), user_id=str(RUN.user_id),
                 conversation_id=str(RUN.conversation_id), run_type="conversation")
    config = {"configurable": {"thread_id": str(RUN.id)}}
    first = await graph.ainvoke(state, config)
    assert first["__interrupt__"]
    assert "decision_references" not in first
    assert "decision_outcome" not in first
    assert factory.handoffs[0]["instruction"] == "安排30分钟总结会议"
    resumed = await graph.ainvoke(Command(resume="approved"), config)
    assert resumed["assistant_content"] == "已批准"
    assert main.calls == 1
    # 页面邮件任务绕过 load_context 和主 Agent。
    state.update(run_type="mail_extraction", run_id=str(UUID(int=100)), conversation_id="")
    direct = await graph.ainvoke(state, {"configurable": {"thread_id": str(UUID(int=100))}})
    assert direct["mail_workflow_completed"]
    assert main.calls == 1


@pytest.mark.asyncio
async def test_main_agent_calls_model_and_validates_json():
    """主 Agent 确实向模型传递受限能力与 Schema，解析模型决定。"""
    class Client:
        async def complete(self, messages):
            assert "输出 Schema" in messages[0].content
            assert "calendar" in messages[0].content
            return '{"decision":"clarify","clarification_question":"要处理哪封邮件？"}'
    decision = await MainAgent(Client()).decide(
        message="那封邮件", history=[], capabilities=[CAPABILITIES[0].model_dump()], references=[])
    assert decision.decision == "clarify"


@pytest.mark.asyncio
async def test_legacy_natural_language_mail_generation_is_downgraded_to_card_navigation():
    """即使模型提出摘要动作，生产主决策也只交接只读卡片导航。"""
    response = MainDecision(decision="delegate", domain="mail", intent="extract_mail",
                            instruction="摘要第一封", target={"resource_type":"email",
                            "id":EMAIL_ID, "source_version":"v1"})
    result = await decide(FakeMain(response), Service(None, None, repository=FakeRepository()),
                          CAPABILITIES, latest_user_message="摘要第一封")
    assert result["workflow"] == "mail_query"
    assert result["handoff"]["intent"] == "query_mail"
    assert result["handoff"]["target"]["id"] == str(EMAIL_ID)
