"""集中策略：可配置局部上限、全局上限、版本恢复及不可变性。"""
from dataclasses import replace, FrozenInstanceError
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from aegis_agent_worker.harness.policy_provider import PolicyProvider, PolicyBundle, LEGACY_POLICY
from aegis_agent_worker.harness.policies.execution import TaskExecutionPolicy
from aegis_agent_worker.harness.policies.workflow import MainContextPolicy, MailCollectionPolicy
from aegis_agent_worker.harness.policies.output import OutputPolicy
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.agent.specialists.main_agent import MainAgent, MainDecisionOutputError
from aegis_agent_worker.agent.specialists.mail_agent import MailAgent, MailAgentError
from aegis_agent_worker.agent.workflows.main.decision_workflow import MainDecisionWorkflow
from aegis_agent_worker.agent.workflows.mail.collection_workflow import MailCollectionWorkflow
from aegis_agent_worker.agent.contracts.react import ReActDecision
from test_harness_react_revision import Database, RUN, Tasks, Inputs
from test_main_card_react import Client, Tools, Audit, CAPS, READ, state


def profile(version, **kwargs):
    return replace(LEGACY_POLICY, execution=replace(LEGACY_POLICY.execution, version=version), **kwargs)


@pytest.mark.parametrize("construct", [
    lambda: MainContextPolicy(max_reads=0), lambda: MainContextPolicy(max_rounds=True),
    lambda: MailCollectionPolicy(max_rounds=-1), lambda: OutputPolicy(max_repairs=2),
    lambda: OutputPolicy(read_tool_retries=-1), lambda: TaskExecutionPolicy(max_tools=0),
    lambda: TaskExecutionPolicy(tool_scopes={}), lambda: PolicyBundle(output={}),
])
def test_invalid_policy_is_rejected_before_startup(construct):
    """非法额度、可变工具范围或策略包类型不能等到执行阶段才报错。"""
    with pytest.raises(ValueError): construct()


def test_profiles_are_immutable_and_duplicate_versions_rejected():
    """策略对象不可变，返回工具集合的修改也不能改写白名单。"""
    with pytest.raises(FrozenInstanceError): LEGACY_POLICY.main_context.max_reads = 99
    scopes = LEGACY_POLICY.execution.tools("mail_send")
    scopes.add("unknown")
    assert "unknown" not in LEGACY_POLICY.execution.tools("mail_send")
    with pytest.raises(ValueError): PolicyProvider((LEGACY_POLICY, LEGACY_POLICY))


@pytest.mark.asyncio
async def test_changed_default_does_not_reset_old_run_or_change_local_policy():
    """同一run恢复读出旧策略及计数，新run才使用新版更大额度。"""
    old = profile("old", main_context=MainContextPolicy(max_reads=1, max_rounds=2))
    old = replace(old, execution=replace(old.execution, max_models=1))
    new = profile("new", main_context=MainContextPolicy(max_reads=4, max_rounds=5))
    db = Database()
    first = HarnessExecutor(db, policy_provider=PolicyProvider((old, new), "old"))
    async with first.scope(RUN):
        async with first.attempt("model", "test"): pass
    upgraded = HarnessExecutor(db, policy_provider=PolicyProvider((old, new), "new"))
    async with upgraded.scope(RUN):
        assert upgraded.policy.max_models == 1
        assert PolicyProvider().current().main_context.max_reads == 1  # 协程内统一使用run固定包
        with pytest.raises(HarnessError) as error:
            async with upgraded.attempt("model", "test"): pass
        assert error.value.code == "EXECUTION_BUDGET_EXCEEDED"
    assert db.budgets[RUN.id]["policy_version"] == "old" and db.budgets[RUN.id]["model_calls"] == 1
    new_run = replace(RUN, id=UUID(int=99))
    async with upgraded.scope(new_run):
        assert upgraded.policy.version == "new"
        assert PolicyProvider().current().main_context.max_reads == 4
    assert db.budgets[new_run.id]["policy_version"] == "new"


@pytest.mark.asyncio
async def test_missing_or_conflicting_pinned_version_fails_before_call():
    """原版本缺失或Checkpoint版本冲突明确失败，不能自动用新版本重置预算。"""
    old, new = profile("old"), profile("new")
    db = Database()
    executor = HarnessExecutor(db, policy_provider=PolicyProvider((old,), "old"))
    async with executor.scope(RUN): pass
    upgraded = HarnessExecutor(db, policy_provider=PolicyProvider((new,), "new"))
    with pytest.raises(HarnessError) as error:
        async with upgraded.scope(RUN): raise AssertionError("不得进入执行")
    assert error.value.code == "POLICY_VERSION_UNAVAILABLE"
    assert db.budgets[RUN.id]["policy_version"] == "old"
    async with executor.scope(RUN):
        with pytest.raises(HarnessError) as error:
            PolicyProvider().current({"execution_policy_version": "new"})
        assert error.value.code == "POLICY_VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_main_local_reads_and_prompt_remaining_use_same_policy():
    """额度改为1时，提示词剩余数与工作流拒绝第二次读取的规则一致。"""
    custom = profile("local", main_context=MainContextPolicy(max_reads=1, max_rounds=3))
    provider = PolicyProvider((custom,), "local")
    client, tools = Client([READ, READ]), Tools()
    graph = MainDecisionWorkflow(MainAgent(client, policy_provider=provider), Audit(), CAPS,
        context_tools=tools, policy_provider=provider).build()
    result = await graph.ainvoke(state())
    assert result["workflow"] == "clarify" and len(tools.calls) == 1
    assert "剩余读取次数：0" in client.calls[1][0].content
    assert result["execution_policy_version"] == "local"


@pytest.mark.asyncio
async def test_local_round_limit_blocks_next_model_request():
    """一轮额度耗尽后不再调用模型，即使读取额度仍有余额。"""
    custom = profile("rounds", main_context=MainContextPolicy(max_reads=2, max_rounds=1))
    provider = PolicyProvider((custom,), "rounds")
    client = Client([READ])
    graph = MainDecisionWorkflow(MainAgent(client, policy_provider=provider), Audit(), CAPS,
        context_tools=Tools(), policy_provider=provider).build()
    result = await graph.ainvoke(state())
    assert result["workflow"] == "clarify" and len(client.calls) == 1


@pytest.mark.asyncio
async def test_output_policy_can_disable_repair_for_main_and_mail():
    """格式修复次数从集中策略读取，关闭后首次失败立即结束，不能调用第二次。"""
    custom = profile("no-repair", output=replace(LEGACY_POLICY.output, max_repairs=0))
    provider = PolicyProvider((custom,), "no-repair")
    for domain in ("main", "mail"):
        client = Client([{"invalid": True}])
        if domain == "main":
            agent = MainAgent(client, policy_provider=provider)
            with pytest.raises(MainDecisionOutputError):
                await agent.decide(message="任务", history=[], capabilities=[], references=[])
        else:
            agent = MailAgent(client, policy_provider=provider)
            with pytest.raises(MailAgentError):
                await agent.collect_information(source={}, evidence=[], answers=[])
        assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_read_retry_setting_and_global_attempt_count_are_consistent():
    """关闭只读重试时失败只占一次工具预算。"""
    from aegis_agent_worker.tool.registry import ToolRegistry
    from aegis_agent_worker.tool.gateway import ToolGateway
    from aegis_agent_worker.tool.contracts import ToolDefinition, ToolError, ToolInvocation
    from aegis_agent_worker.agent.contracts.context import CardsGetArgs
    custom = profile("no-retry", output=replace(LEGACY_POLICY.output, read_tool_retries=0))
    provider = PolicyProvider((custom,), "no-retry")
    db, registry = Database(), ToolRegistry()
    class Handler:
        calls = 0
        async def invoke(self, *args):
            self.calls += 1
            raise ToolError("TOOL_UNAVAILABLE", "不可用")
    handler = Handler()
    registry.register(ToolDefinition(name="conversation.cards.get", version="1", risk_level="read",
        mcp_server="internal", description="卡片", argument_schema=CardsGetArgs), handler)
    harness = HarnessExecutor(db, policy_provider=provider)
    gateway = ToolGateway(registry, harness=harness)
    run = replace(RUN, run_type="conversation")
    async with harness.scope(run):
        prepared = await gateway.prepare(None, tenant_id=run.tenant_id, user_id=run.user_id, run_id=run.id,
            invocation=ToolInvocation("conversation.cards.get", {"selector": "latest"}))
        with pytest.raises(ToolError): await gateway.invoke(prepared)
    assert handler.calls == 1 and db.budgets[run.id]["tool_calls"] == 1


@pytest.mark.asyncio
async def test_card_single_result_limit_applies_to_get_and_list():
    """集中单次字节额度约束get和list，不能只约束主图累计结果。"""
    from test_card_context_tools import Database as CardDatabase, Repository, CONTEXT
    from aegis_agent_worker.service.runtime.card_context_service import CardContextService
    from aegis_agent_worker.tool.contracts import ToolError
    custom = profile("small-card", output=replace(LEGACY_POLICY.output, card_result_bytes=10))
    service = CardContextService(CardDatabase(), Repository(), PolicyProvider((custom,), "small-card"))
    for tool, arguments in (("conversation.cards.list", {}), ("conversation.cards.get", {"selector": "latest"})):
        with pytest.raises(ToolError) as error: await service.read(CONTEXT, tool, arguments)
        assert error.value.code == "CARD_CONTEXT_TOO_LARGE"


@pytest.mark.asyncio
async def test_mail_checkpoint_resume_keeps_old_local_round_limit():
    """检查点保留旧局部策略版本，升级默认值后回答不能获得更多收集轮次。"""
    old = profile("old-mail", mail_collection=MailCollectionPolicy(max_rounds=1))
    new = profile("new-mail", mail_collection=MailCollectionPolicy(max_rounds=10))
    class Agent:
        calls = 0
        async def collect_information(self, **kwargs):
            self.calls += 1
            return ReActDecision(action="ask_user", question="报价是多少？")
    agent, saver = Agent(), MemorySaver()
    # 编译图应由同一StateGraph设置checkpointer，使用父图与生产一致。
    from langgraph.graph import StateGraph, START, END
    from aegis_agent_worker.agent.graph.state import TaskGraphState
    def root(default):
        graph = StateGraph(TaskGraphState)
        graph.add_node("collect", MailCollectionWorkflow(agent, Tasks(), None, Inputs(),
                       PolicyProvider((old, new), default)).build())
        graph.add_edge(START, "collect"); graph.add_edge("collect", END)
        return graph.compile(checkpointer=saver)
    initial = dict(run_id=str(RUN.id), tenant_id=str(RUN.tenant_id), user_id=str(RUN.user_id),
                   run_type="mail_reply_draft", mail_decision={"target": {"source_version": "v1"}})
    config = {"configurable": {"thread_id": "policy-resume"}}
    assert (await root("old-mail").ainvoke(initial, config))["__interrupt__"]
    with pytest.raises(HarnessError) as error:
        await root("new-mail").ainvoke(Command(resume={"question_id": str(UUID(int=20)), "response": "100元"}), config)
    assert error.value.code == "REACT_BUDGET_EXCEEDED" and agent.calls == 1
