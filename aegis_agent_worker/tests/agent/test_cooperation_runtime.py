"""8.1：真实团队图和Harness，专家使用显式测试图，非生产固定链。"""
import json
from dataclasses import replace
from typing import Any, TypedDict
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, END, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from aegis_agent_worker.agent.contracts.cooperation.decisions import CooperationDecision
from aegis_agent_worker.agent.contracts.cooperation.messages import ExpertResult, DelegationMessage
from aegis_agent_worker.agent.contracts.decision import MainDecision, AgentHandoff
from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.routing.team_registry import ExpertBinding, TeamRegistry
from aegis_agent_worker.agent.specialists.cooperation_agent import CooperationAgent
from aegis_agent_worker.agent.workflows.cooperation.factory import CooperationWorkflowFactory
from aegis_agent_worker.agent.workflows.cooperation.team_workflow import CooperationTeamWorkflow
from aegis_agent_worker.harness.context import bind_execution, reset_execution
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.harness.policies.cooperation import CooperationPolicy
from aegis_agent_worker.harness.policy_provider import (
    COOPERATION_CONTRACT_POLICY, LEGACY_POLICY, PolicyProvider,
)
from aegis_agent_worker.llm.client import LLMError
from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
from aegis_agent_worker.llm.usage import model_call_purpose
from aegis_agent_worker.repository.run_repository import ClaimedRun
from test_harness_react_revision import Database


RUN = ClaimedRun(id=UUID(int=81), tenant_id=UUID(int=82), user_id=UUID(int=83),
                 conversation_id=UUID(int=84), run_type="conversation")
DRAFT = dict(resource_type="calendar_draft", id=str(UUID(int=85)), version=1)
MAIL = dict(resource_type="email", id=str(UUID(int=86)), source_version="source-v1")
DELEGATE_MAIL = dict(action="delegate", expert="mail", goal="查明续约邮件中的会议要求")
DELEGATE_CALENDAR = dict(action="delegate", expert="calendar", goal="按要求评估时间并保存草稿")


def state():
    return dict(run_id=str(RUN.id), tenant_id=str(RUN.tenant_id), user_id=str(RUN.user_id),
        conversation_id=str(RUN.conversation_id), run_type="conversation",
        handoff=AgentHandoff(domain="cooperation", intent="mail_calendar_meeting",
            instruction="请按续约邮件中的时间要求查询日历并安排会议草稿").model_dump(mode="json"),
        history=[{"role": "user", "content": "不该共享的局部历史"}])


class Client:
    provider_name = model_name = "test"

    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    async def complete(self, messages):
        raise AssertionError("协调Agent必须使用JSON接口")

    async def complete_json(self, messages):
        self.calls.append(messages)
        result = next(self.outputs)
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


class Events:
    def __init__(self):
        self.items = []

    async def record(self, run, **kwargs):
        self.items.append((run, kwargs))


class ExpertState(TypedDict, total=False):
    run_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    run_type: str
    execution_policy_version: str
    delegation: dict[str, Any]
    team_context: dict[str, Any]
    expert_result: dict[str, Any]


class Expert:
    def __init__(self, name, callback=None, valid=True):
        self.name, self.callback, self.valid = name, callback, valid
        self.calls, self.verifications = [], []

    async def invoke(self, payload):
        self.calls.append(payload)
        extra = await self.callback(payload) if self.callback else {}
        return {"expert_result": {"delegation_id": payload["delegation"]["delegation_id"],
            "expert": self.name, "status": "completed", "summary": f"{self.name}领域结果",
            "references": [MAIL] if self.name == "mail" else [],
            "draft": DRAFT if self.name == "calendar" else None, **extra}}

    async def verify(self, run, delegation, result):
        self.verifications.append((run, delegation, result))
        return self.valid

    def binding(self):
        graph = StateGraph(ExpertState)
        graph.add_node("expert_local_loop", self.invoke)
        graph.add_edge(START, "expert_local_loop")
        graph.add_edge("expert_local_loop", END)
        return ExpertBinding(self.name, f"{self.name}专家", graph.compile(), self.verify,
                             can_return_draft=self.name == "calendar")


def setup(outputs, *, profile=COOPERATION_CONTRACT_POLICY, mail=None, calendar=None):
    provider = PolicyProvider((LEGACY_POLICY, profile), profile.version)
    db, events = Database(), Events()
    harness = HarnessExecutor(db, policy_provider=provider)
    client = Client(outputs)
    guarded = GuardedLLMClient(client, harness)
    agent = CooperationAgent(guarded, provider)
    mail, calendar = mail or Expert("mail"), calendar or Expert("calendar")
    registry = TeamRegistry((mail.binding(), calendar.binding()))
    workflow = CooperationTeamWorkflow(agent, registry, events, provider)
    return workflow, harness, client, db, events, mail, calendar


@pytest.mark.parametrize("payload", [
    {"action": "send_mail"}, {"action": "delegate", "expert": "mail"},
    {"action": "delegate", "expert": "mail", "goal": "   "},
    {"action": "finish", "goal": "已写入"},
    {**DELEGATE_MAIL, "tool_name": "mail.messages.send"},
    {**DELEGATE_MAIL, "tenant_id": str(UUID(int=1))},
    {"action": "finish", "approved": True},
])
def test_decision_rejects_tools_identity_and_illegal_combinations(payload):
    with pytest.raises(ValidationError):
        CooperationDecision.model_validate(payload)


def test_capability_schema_does_not_allow_cooperation_to_forge_targets():
    valid = MainDecision(decision="delegate", domain="cooperation", intent="mail_calendar_meeting",
                         instruction="按邮件要求安排会议")
    assert valid.intent == "mail_calendar_meeting"
    with pytest.raises(ValidationError):
        MainDecision.model_validate({**valid.model_dump(), "target": MAIL})
    with pytest.raises(ValidationError):
        MainDecision.model_validate({**valid.model_dump(), "domain": "mail"})


def test_registry_requires_real_bindings_and_rejects_duplicate_experts():
    with pytest.raises(ValueError):
        TeamRegistry([Expert("mail").binding()]).require_ready()
    with pytest.raises(ValueError):
        TeamRegistry([Expert("mail").binding(), Expert("mail").binding()])
    with pytest.raises(ValueError):
        ExpertBinding("web", "联网", object(), lambda *args: True)
    with pytest.raises(ValueError):
        ExpertBinding("calendar", "日历", object(), lambda *args: True, can_return_draft=True)
    with pytest.raises(TypeError):
        CooperationAgent(Client([]))


@pytest.mark.parametrize("kwargs", [dict(max_rounds=True), dict(max_delegations=6),
    dict(max_context_bytes=0), dict(max_repeated_results=0), dict(enabled="yes")])
def test_policy_rejects_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        CooperationPolicy(**kwargs)


@pytest.mark.asyncio
async def test_team_observes_results_and_does_not_share_root_history():
    workflow, harness, client, db, events, mail, calendar = setup([
        DELEGATE_MAIL, DELEGATE_CALENDAR, {"action": "finish"}])
    async with harness.scope(RUN):
        result = await workflow.build().ainvoke(state())
    assert result["cooperation_status"] == "draft_ready"
    assert result["cooperation_draft"]["id"] == DRAFT["id"]
    assert len(mail.calls) == len(calendar.calls) == 1
    assert not calendar.calls[0].keys() & {"history", "handoff", "approval_decision", "mail_body"}
    assert calendar.calls[0]["team_context"]["results"][0]["expert"] == "mail"
    assert 'mail领域结果' in client.calls[1][1].content
    assert db.budgets[RUN.id]["model_calls"] == 3
    assert len(mail.verifications) > 1  # 交接后及结束前再次核验事实。
    assert [item[1]["phase"] for item in events.items] == [
        "started", "coordinating", "delegated", "expert_started", "expert_completed", "coordinating",
        "delegated", "expert_started", "expert_completed", "coordinating", "draft_ready", "finished"]
    assert "awaiting_confirmation" not in result  # 团队阶段不是审批状态写入者。


@pytest.mark.asyncio
async def test_existing_context_can_delegate_calendar_without_fixed_mail_step():
    workflow, harness, client, db, events, mail, calendar = setup([DELEGATE_CALENDAR, {"action": "finish"}])
    async with harness.scope(RUN):
        result = await workflow.build().ainvoke(state())
    assert result["cooperation_status"] == "draft_ready" and not mail.calls
    assert len(calendar.calls) == 1


@pytest.mark.asyncio
async def test_coordinator_can_redelegate_based_on_expert_result():
    workflow, harness, client, db, events, mail, calendar = setup([
        DELEGATE_MAIL, DELEGATE_MAIL, DELEGATE_CALENDAR, {"action": "finish"}])
    async with harness.scope(RUN):
        result = await workflow.build().ainvoke(state())
    assert [item["target"] for item in result["cooperation_delegations"]] == ["mail", "mail", "calendar"]
    assert len(mail.calls) == 2 and len(calendar.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,code", [
    ({"action": "finish"}, "COOPERATION_MISSING_DRAFT"),
    ({"action": "finish_unavailable"}, "COOPERATION_UNAVAILABLE"),
    ({**DELEGATE_MAIL, "expert": "web"}, "COOPERATION_UNKNOWN_EXPERT"),
])
async def test_invalid_completion_or_unregistered_recipient_never_runs_experts(decision, code):
    workflow, harness, client, db, events, mail, calendar = setup([decision])
    async with harness.scope(RUN):
        result = await workflow.build().ainvoke(state())
    assert result["cooperation_error_code"] == code
    assert not mail.calls and not calendar.calls and not db.budgets[RUN.id]["tool_calls"]


@pytest.mark.asyncio
async def test_stale_result_blocks_following_decision_and_finish():
    workflow, harness, client, db, events, mail, calendar = setup([DELEGATE_CALENDAR], calendar=Expert("calendar", valid=False))
    async with harness.scope(RUN):
        result = await workflow.build().ainvoke(state())
    assert result["cooperation_error_code"] == "COOPERATION_RESULT_STALE"
    assert result["cooperation_draft"] == {} and len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"delegation_id": str(UUID(int=1))}, {"expert": "calendar"}, {"draft": DRAFT},
    {"raw_mail_body": "不得写入团队状态"},
])
async def test_untrusted_expert_output_is_rejected_before_checkpoint_result(extra):
    async def malformed(payload):
        return extra
    workflow, harness, client, db, events, mail, calendar = setup([DELEGATE_MAIL], mail=Expert("mail", malformed))
    async with harness.scope(RUN):
        result = await workflow.build().ainvoke(state())
    assert result["cooperation_status"] == "unavailable" and not result["cooperation_results"]
    assert "raw_mail_body" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_bounded_format_repair_does_not_echo_invalid_output():
    secret = "非法输出不能进入修复提示"
    workflow, harness, client, db, events, mail, calendar = setup([secret, {"action": "finish_unavailable"}])
    async with harness.scope(RUN):
        result = await workflow.build().ainvoke(state())
    assert result["cooperation_status"] == "unavailable" and len(client.calls) == 2
    assert secret not in str(client.calls[1])
    assert db.budgets[RUN.id]["model_calls"] == 2


@pytest.mark.asyncio
async def test_repeated_invalid_output_stops_without_tools():
    workflow, harness, client, db, events, mail, calendar = setup(["", {"action": "unknown"}])
    async with harness.scope(RUN):
        result = await workflow.build().ainvoke(state())
    assert result["cooperation_error_code"] == "COOPERATION_INVALID_DECISION"
    assert len(client.calls) == 2 and not mail.calls and not calendar.calls


@pytest.mark.asyncio
async def test_network_failure_is_not_repaired_or_fallback_to_expert():
    workflow, harness, client, db, events, mail, calendar = setup([LLMError("network")])
    async with harness.scope(RUN):
        with pytest.raises(LLMError):
            await workflow.build().ainvoke(state())
    assert len(client.calls) == 1 and not mail.calls
    assert events.items[-1][1]["phase"] == "failed"


@pytest.mark.asyncio
async def test_repair_reserves_same_persisted_budget_before_request():
    profile = replace(COOPERATION_CONTRACT_POLICY,
        execution=replace(COOPERATION_CONTRACT_POLICY.execution, max_models=1))
    workflow, harness, client, db, events, mail, calendar = setup(["invalid"], profile=profile)
    async with harness.scope(RUN):
        with pytest.raises(HarnessError) as error:
            await workflow.build().ainvoke(state())
    assert error.value.code == "EXECUTION_BUDGET_EXCEEDED" and len(client.calls) == 1


@pytest.mark.asyncio
async def test_expert_llm_and_coordinator_share_the_same_budget():
    profile = replace(COOPERATION_CONTRACT_POLICY,
        execution=replace(COOPERATION_CONTRACT_POLICY.execution, max_models=2))
    workflow, harness, client, db, events, mail, calendar = setup([
        DELEGATE_MAIL, {"fact": "专家调用"}], profile=profile)
    async def local_model(payload):
        with model_call_purpose("mail_collection"):
            await workflow._agent._client.complete_json([])
        return {}
    mail.callback = local_model
    async with harness.scope(RUN):
        with pytest.raises(HarnessError) as error:
            await workflow.build().ainvoke(state())
    assert error.value.code == "EXECUTION_BUDGET_EXCEEDED"
    assert len(client.calls) == 2 and db.budgets[RUN.id]["model_calls"] == 2


@pytest.mark.asyncio
async def test_local_delegation_limit_stops_before_second_expert():
    profile = replace(COOPERATION_CONTRACT_POLICY,
        cooperation=CooperationPolicy(enabled=True, max_delegations=1, max_rounds=3))
    workflow, harness, client, db, events, mail, calendar = setup([DELEGATE_MAIL, DELEGATE_CALENDAR], profile=profile)
    async with harness.scope(RUN):
        result = await workflow.build().ainvoke(state())
    assert result["cooperation_error_code"] == "COOPERATION_DELEGATION_LIMIT" and not calendar.calls


@pytest.mark.asyncio
async def test_no_progress_and_context_bytes_stop_loop():
    workflow, harness, client, db, events, mail, calendar = setup([DELEGATE_MAIL]*3)
    async with harness.scope(RUN):
        result = await workflow.build().ainvoke(state())
    assert result["cooperation_error_code"] == "COOPERATION_NO_PROGRESS" and len(client.calls) == 3
    profile = replace(COOPERATION_CONTRACT_POLICY,
        cooperation=replace(COOPERATION_CONTRACT_POLICY.cooperation, max_context_bytes=10))
    workflow, harness, client, db, events, mail, calendar = setup([], profile=profile)
    async with harness.scope(RUN):
        result = await workflow.build().ainvoke(state())
    assert result["cooperation_error_code"] == "COOPERATION_CONTEXT_TOO_LARGE" and not client.calls


@pytest.mark.asyncio
async def test_disabled_or_mismatched_runtime_cannot_make_model_request():
    workflow, harness, client, db, events, mail, calendar = setup([])
    token = bind_execution(RUN, LEGACY_POLICY)
    try:
        with pytest.raises(HarnessError) as error:
            await workflow.build().ainvoke(state())
        assert error.value.code == "COOPERATION_POLICY_DISABLED"
    finally:
        reset_execution(token)
    async with harness.scope(RUN):
        with pytest.raises(HarnessError) as error:
            await workflow.build().ainvoke({**state(), "tenant_id": str(UUID(int=99))})
    assert error.value.code == "COOPERATION_IDENTITY_MISMATCH" and not client.calls


@pytest.mark.asyncio
async def test_nested_expert_interrupt_resumes_same_delegation_without_restarting_team():
    async def ask(payload):
        answer = interrupt({"question_id": "test-question", "expert": "mail"})
        assert answer == "选择第一封"
        return {}
    workflow, harness, client, db, events, mail, calendar = setup([
        DELEGATE_MAIL, {"action": "finish_unavailable"}], mail=Expert("mail", ask))
    parent = StateGraph(TaskGraphState)
    parent.add_node("cooperation", workflow.build())
    parent.add_edge(START, "cooperation")
    parent.add_edge("cooperation", END)
    graph = parent.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": str(RUN.id)}}
    async with harness.scope(RUN):
        paused = await graph.ainvoke(state(), config)
    assert paused.get("__interrupt__") and len(client.calls) == 1
    async with harness.scope(RUN):
        result = await graph.ainvoke(Command(resume="选择第一封"), config)
    assert result["cooperation_status"] == "unavailable" and len(client.calls) == 2
    assert len({item["delegation"]["delegation_id"] for item in mail.calls}) == 1
    assert [item[1]["phase"] for item in events.items].count("started") == 1
    assert [item[1]["phase"] for item in events.items].count("delegated") == 1
    assert db.budgets[RUN.id]["model_calls"] == 2


@pytest.mark.asyncio
async def test_factory_requires_approval_and_routes_ready_draft_to_deterministic_continuation():
    workflow, harness, client, db, events, mail, calendar = setup([DELEGATE_CALENDAR, {"action": "finish"}])
    registry = GraphRegistry()
    with pytest.raises(ValueError):
        CooperationWorkflowFactory(workflow._agent, workflow._registry, events, None, workflow._policies).register(registry)
    assert not registry.names()
    approved = []
    async def continuation(payload):
        approved.append(payload["cooperation_draft"])
        return {"assistant_content": "确定性确认续接（测试替身）"}
    graph = StateGraph(TaskGraphState)
    graph.add_node("continue_approval", continuation)
    graph.add_edge(START, "continue_approval")
    graph.add_edge("continue_approval", END)
    CooperationWorkflowFactory(workflow._agent, workflow._registry, events,
        graph.compile(), workflow._policies).register(registry)
    assert registry.capabilities()[0].domain == "cooperation"
    async with harness.scope(RUN):
        result = await registry.get("cooperation_meeting").ainvoke(state())
    assert approved[0]["id"] == DRAFT["id"] and len(client.calls) == 2
    assert result["assistant_content"].startswith("确定性")


@pytest.mark.asyncio
async def test_final_result_is_reverified_after_model_finish_decision():
    workflow, harness, client, db, events, mail, calendar = setup([DELEGATE_CALENDAR, {"action": "finish"}])
    original = client.complete_json
    async def invalidate_before_finish(messages):
        output = await original(messages)
        if json.loads(output)["action"] == "finish":
            calendar.valid = False
        return output
    client.complete_json = invalidate_before_finish
    async with harness.scope(RUN):
        result = await workflow.build().ainvoke(state())
    assert result["cooperation_error_code"] == "COOPERATION_RESULT_STALE"
    assert result["cooperation_draft"] == {}
    assert "draft_ready" not in [item[1]["phase"] for item in events.items]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["approved", "rejected"])
async def test_approval_resume_does_not_rerun_team_models(answer):
    workflow, harness, client, db, events, mail, calendar = setup([DELEGATE_CALENDAR, {"action": "finish"}])
    writes = []
    async def wait_for_confirmation(payload):
        decision = interrupt({"status": "waiting_confirmation", "draft_id": payload["cooperation_draft"]["id"]})
        if decision == "approved":
            writes.append(payload["cooperation_draft"])
        return {"assistant_content": "已批准" if decision == "approved" else "已拒绝"}
    approval = StateGraph(TaskGraphState)
    approval.add_node("wait", wait_for_confirmation)
    approval.add_edge(START, "wait")
    approval.add_edge("wait", END)
    registry = GraphRegistry()
    CooperationWorkflowFactory(workflow._agent, workflow._registry, events,
        approval.compile(), workflow._policies).register(registry)
    parent = StateGraph(TaskGraphState)
    parent.add_node("team_entry", registry.get("cooperation_meeting"))
    parent.add_edge(START, "team_entry")
    parent.add_edge("team_entry", END)
    graph = parent.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": str(RUN.id)}}
    async with harness.scope(RUN):
        first = await graph.ainvoke(state(), config)
    assert first.get("__interrupt__") and len(client.calls) == 2 and not writes
    async with harness.scope(RUN):
        result = await graph.ainvoke(Command(resume=answer), config)
    assert len(client.calls) == 2 and len(calendar.calls) == 1
    assert len(writes) == (1 if answer == "approved" else 0)
    assert result["assistant_content"] == ("已批准" if answer == "approved" else "已拒绝")


@pytest.mark.asyncio
async def test_event_service_persists_safe_payload_before_publish_after_commit():
    from contextlib import asynccontextmanager
    from aegis_agent_worker.service.cooperation.team_event_service import TeamEventService
    order, persisted = [], []
    class Session:
        @asynccontextmanager
        async def begin(self):
            order.append("begin")
            yield
            order.append("commit")
        async def execute(self, sql, arguments):
            from test_harness_react_revision import Result
            return Result()
    class EventDatabase:
        @asynccontextmanager
        async def session(self):
            yield Session()
    class Runs:
        async def append_event(self, session, **kwargs):
            order.append("persist")
            persisted.append(kwargs)
            return kwargs
    class Publisher:
        def publish_after_commit(self, event):
            # 与现有Publisher协议相同：仅登记提交后发布，不在此网络发送。
            order.append("schedule_after_commit")
    service = TeamEventService(EventDatabase(), Runs(), Publisher())
    from uuid import uuid5
    delegation = DelegationMessage(delegation_id=uuid5(RUN.id,"team-delegation:1"), sequence=1,
        target="mail", goal="秘密目标不应进入进度事件")
    await service.record(RUN, phase="delegated", round_no=1,
        policy_version=COOPERATION_CONTRACT_POLICY.version, delegation=delegation)
    assert order == ["begin", "persist", "schedule_after_commit", "commit"]
    payload = persisted[0]["payload"]
    assert set(payload) == {"version", "phase", "round", "execution_policy_version",
                            "delegation_id", "agent_id", "sequence","label","cooperation","cooperation_event_id"}
    assert "秘密目标" not in json.dumps(payload, ensure_ascii=False)
    # 未配置Redis发布器时仍持久化，不能因为无实时推送而缺少审计。
    await TeamEventService(EventDatabase(), Runs()).record(RUN, phase="started", round_no=0,
        policy_version=COOPERATION_CONTRACT_POLICY.version)
    assert len(persisted) == 2
