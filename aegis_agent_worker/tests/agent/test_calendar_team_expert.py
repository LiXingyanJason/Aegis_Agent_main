"""8.3真实日历Agent/专家/服务/Gateway；模型、SQL存储和提供商均使用替身。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4
import json

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, END, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

from aegis_agent_worker.agent.contracts.calendar import extract_calendar_free_time_candidates
from aegis_agent_worker.agent.contracts.cooperation.calendar import CalendarTeamDecision
from aegis_agent_worker.agent.contracts.cooperation.messages import DelegationMessage, ExpertResult
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.specialists.calendar_agent import CalendarAgent
from aegis_agent_worker.agent.workflows.calendar.approval_workflow import CalendarApprovalWorkflow
from aegis_agent_worker.agent.workflows.calendar.experts.meeting_draft_workflow import CalendarMeetingDraftWorkflow
from aegis_agent_worker.agent.workflows.calendar.experts.state import CalendarExpertState
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.harness.policy_provider import COOPERATION_CALENDAR_POLICY, LEGACY_POLICY, PolicyProvider
from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
from aegis_agent_worker.service.calendar.calendar_draft_service import CalendarDraftService
from aegis_agent_worker.service.calendar.calendar_expert_service import CalendarExpertService
from aegis_agent_worker.service.calendar.calendar_tool_execution_service import CalendarToolExecutionService
from aegis_agent_worker.tool.contracts import ToolDefinition, ToolError, ToolResult
from aegis_agent_worker.tool.gateway import ToolGateway
from aegis_agent_worker.tool.registry import ToolRegistry
from test_cooperation_runtime import Client, RUN
from test_harness_react_revision import Database
from test_mail_meeting_requirements import FACTS, Inputs, Calls, reference


QUERY = dict(action="query_availability", window_index=0)
GENERATE = dict(action="generate_draft")
UNAVAILABLE = dict(action="finish_unavailable")


def proposal(day="2026-10-26"):
    slot = dict(start_at=f"{day}T09:00:00+08:00", end_at=f"{day}T17:00:00+08:00")
    candidate = extract_calendar_free_time_candidates(dict(available_slots=[slot]))[0][0]
    return dict(title=FACTS["title"], description="续约会议草稿", attendees=[],
        selected_slot_id=candidate.slot_id, alternative_slot_ids=[], reason="符合已核验条件")


def initial():
    source = ExpertResult(delegation_id=uuid4(),expert="mail",status="completed",summary="来源要求",
        references=[reference()],meeting_requirements=FACTS)
    return dict(run_id=str(RUN.id),tenant_id=str(RUN.tenant_id),user_id=str(RUN.user_id),
        conversation_id=str(RUN.conversation_id),run_type="conversation",
        delegation=DelegationMessage(delegation_id=UUID(int=911),sequence=2,target="calendar",
            goal="评估可用时间并生成站内会议草稿").model_dump(mode="json"),
        team_context=dict(goal="按续约邮件要求安排会议草稿",results=[source.model_dump(mode="json")]))


class CalendarClient(Client):
    async def complete(self,messages):
        return await self.complete_json(messages)


class Provider:
    def __init__(self):
        self.active=True
        self.free=True
        self.conflict=False
        self.outside=False
        self.calls=[]

    async def find_active_connection(self,session,**kwargs):
        assert kwargs["required_scope"]=="calendar.read"
        assert kwargs.get("required_connection_id") in {None,UUID(int=89)}
        if not self.active:
            return None
        return SimpleNamespace(id=UUID(int=89))

    async def invoke(self,context,arguments):
        self.calls.append((context,arguments))
        if arguments["__tool_name"]=="calendar.list_events":
            payload=dict(query_range=arguments,events=([dict(start_at=arguments["start_at"],end_at=arguments["end_at"])] if self.conflict else []))
        else:
            assert arguments["__tool_name"]=="calendar.find_free_time"
            start=datetime.fromisoformat(arguments["start_at"])
            if self.outside:
                start-=timedelta(days=1)
            payload=dict(duration_minutes=arguments["duration_minutes"],available_slots=([
                dict(start_at=start.isoformat(),end_at=(start+timedelta(hours=8)).isoformat())] if self.free else []))
        return ToolResult(payload,payload)


class DraftRepository:
    def __init__(self,provider):
        self.rows={}
        self.approvals={}
        self.provider=provider
        self.status="running"
        self.inserts=0
        self.version=1

    async def lock_run(self,session,run):
        return self.status in {"running","waiting_confirmation"}

    async def connection(self,session,run,connection_id):
        return SimpleNamespace(id=connection_id) if self.provider.active and connection_id==UUID(int=89) else None

    async def load(self,session,run,draft_id):
        row=self.rows.get(draft_id)
        if not row or run.user_id!=RUN.user_id or run.tenant_id!=RUN.tenant_id or run.id!=RUN.id:
            return None
        return {**deepcopy(row),"readable":self.provider.active,"writable":self.provider.active}

    async def insert(self,session,**kwargs):
        self.inserts+=1
        draft_id=kwargs["draft_id"]
        assert draft_id not in self.rows
        self.rows[draft_id]={**kwargs,"id":draft_id,"version":self.version}
        return draft_id

    async def approval(self,session,run,draft_id,version):
        return self.approvals.get(draft_id)

    async def create_approval(self,session,**kwargs):
        assert kwargs["draft_id"] not in self.approvals
        approval_id=uuid4()
        self.approvals[kwargs["draft_id"]]=dict(id=approval_id,status="pending",valid=True,title=kwargs["title"],
            preview_snapshot=dict(title=kwargs["title"],recommendation=kwargs["recommendation_preview"]))
        return approval_id

    async def mark_pending(self,session,run,draft_id,version):
        assert self.rows[draft_id]["version"]==version
        self.rows[draft_id]["status"]="pending_approval"

    async def mark_ready(self,session,run,approval_id):
        self.status="waiting_confirmation"
        return True

    async def completed_execution(self,session,run,draft_id,approval_id):
        return None


class Runs:
    def __init__(self,repository):
        self.repository=repository
        self.events=[]
        self.steps=[]

    async def create_running_tool_step(self,session,**kwargs):
        assert kwargs["step_key"] not in self.steps
        self.steps.append(kwargs["step_key"])
        return uuid4()

    async def finish_step(self,session,**kwargs):
        pass

    async def append_event(self,session,**kwargs):
        self.events.append(kwargs)
        if kwargs["event_type"]=="cooperation_calendar_draft_saved":
            self.repository.rows[UUID(kwargs["payload"]["draft_id"])]["metadata"]=kwargs["payload"]
        return kwargs


class Sources:
    def __init__(self):
        self.valid=True
        self.calls=[]
    async def describe(self,run,reference):
        self.calls.append(reference)
        if not self.valid:
            raise ToolError("MAIL_SOURCE_CHANGED","来源已变化")
        return {}


def setup(outputs,profile=COOPERATION_CALENDAR_POLICY):
    db,provider,inputs,sources=Database(),Provider(),Inputs(),Sources()
    policies=PolicyProvider((LEGACY_POLICY,profile),profile.version)
    harness=HarnessExecutor(db,policy_provider=policies)
    client=CalendarClient(outputs)
    repository=DraftRepository(provider)
    runs=Runs(repository)
    registry=ToolRegistry()
    for name in ("calendar.list_events","calendar.find_free_time"):
        registry.register(ToolDefinition(name,"1","read","calendar","日历读取",
            connection_providers=("google_calendar","outlook_calendar"),required_connection_scope="calendar.read"),provider)
    tools=ToolGateway(registry,connections=provider,harness=harness,policy_provider=policies)
    queries=CalendarExpertService(CalendarToolExecutionService(db,runs,tools,Calls()))
    drafts=CalendarDraftService(db,runs,sources,repository=repository)
    agent=CalendarAgent(llm_client=GuardedLLMClient(client,harness),policy_provider=policies)
    workflow=CalendarMeetingDraftWorkflow(agent,queries,drafts,inputs,policy_provider=policies)
    return SimpleNamespace(db=db,provider=provider,inputs=inputs,sources=sources,policies=policies,harness=harness,
        client=client,repository=repository,runs=runs,queries=queries,drafts=drafts,workflow=workflow)


def root(workflow):
    graph=StateGraph(CalendarExpertState)
    graph.add_node("calendar",workflow.build())
    graph.add_edge(START,"calendar")
    graph.add_edge("calendar",END)
    return graph.compile(checkpointer=MemorySaver())


@pytest.mark.parametrize("payload",[
    dict(action="calendar.create_event"),dict(action="query_availability",window_index=True),
    dict(action="query_availability",window_index=0,approved=True),dict(action="generate_draft",proposed_requirements=FACTS),
    dict(action="ask_user",question="请提供密码"),dict(action="generate_draft",window_index=1),
])
def test_actions_do_not_accept_permissions_arbitrary_dates_or_external_writes(payload):
    with pytest.raises(ValidationError):
        CalendarTeamDecision.model_validate(payload)


@pytest.mark.asyncio
async def test_real_calendar_expert_queries_checks_and_saves_only_internal_draft():
    env=setup([QUERY,GENERATE,proposal()])
    async with env.harness.scope(RUN):
        result=await env.workflow.build().ainvoke(initial())
    output=ExpertResult.model_validate(result["expert_result"])
    assert output.status=="completed" and output.draft.version==1
    assert env.repository.inserts==1 and not env.repository.approvals and env.repository.status=="running"
    assert env.db.budgets[RUN.id]["model_calls"]==3 and env.db.budgets[RUN.id]["tool_calls"]==2
    assert [item[1]["__tool_name"] for item in env.provider.calls]==["calendar.find_free_time","calendar.list_events"]
    row=env.repository.rows[output.draft.id]
    assert row["status"]=="draft" and row["end_at"]-row["start_at"]==timedelta(minutes=50)
    assert not any(item["event_type"]=="approval_required" for item in env.runs.events)
    assert env.workflow.binding().can_return_draft
    assert await env.drafts.verify_result(RUN,DelegationMessage.model_validate(initial()["delegation"]),output)


@pytest.mark.asyncio
async def test_saved_draft_replay_does_not_regenerate_or_duplicate():
    env=setup([QUERY,GENERATE,proposal()])
    async with env.harness.scope(RUN):
        result=await env.workflow.build().ainvoke(initial())
        replay=await env.workflow._generate(result)
    assert replay["expert_result"]["draft"]==result["expert_result"]["draft"]
    assert env.repository.inserts==1 and len(env.client.calls)==3 and len(env.provider.calls)==2


@pytest.mark.asyncio
@pytest.mark.parametrize("answer",["确认调整","拒绝","直接创建日历"])
async def test_no_slots_condition_change_is_not_effective_until_explicit_confirmation(answer):
    updated={**FACTS,"time_windows":[dict(start_at="2026-10-27T09:00:00+08:00",end_at="2026-10-27T17:00:00+08:00")]}
    env=setup([QUERY,dict(action="ask_user",question="是否改到次日？",proposed_requirements=updated),
               QUERY,GENERATE,proposal("2026-10-27" if answer=="确认调整" else "2026-10-26")])
    env.provider.free=False
    graph=root(env.workflow)
    config={"configurable":{"thread_id":str(RUN.id)}}
    async with env.harness.scope(RUN):
        first=await graph.ainvoke(initial(),config)
    request=first["__interrupt__"][0].value
    assert "2026-10-27" in request["question"] and "不是批准" in request["question"]
    assert len(env.provider.calls)==1 and not env.repository.rows
    env.provider.free=True
    async with env.harness.scope(RUN):
        result=await graph.ainvoke(Command(resume=dict(question_id=request["question_id"],response=answer)),config)
    expected="2026-10-27" if answer=="确认调整" else "2026-10-26"
    assert expected in env.provider.calls[1][1]["start_at"]
    assert result["expert_result"]["status"]=="completed" and not env.repository.approvals


@pytest.mark.asyncio
async def test_resume_rechecks_sources_before_any_new_model_or_tool():
    env=setup([QUERY,dict(action="ask_user",question="请补充可接受的时间条件")])
    graph=root(env.workflow)
    config={"configurable":{"thread_id":str(RUN.id)}}
    async with env.harness.scope(RUN):
        first=await graph.ainvoke(initial(),config)
    request=first["__interrupt__"][0].value
    env.sources.valid=False
    async with env.harness.scope(RUN):
        result=await graph.ainvoke(Command(resume=dict(question_id=request["question_id"],response="保持原条件")),config)
    assert result["expert_result"]["error_code"]=="MAIL_SOURCE_CHANGED"
    assert len(env.client.calls)==2 and len(env.provider.calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize("patch",[dict(selected_slot_id="slot_deadbeef"),dict(attendees=["forged@example.com"]),dict(title="擅自改主题")])
async def test_forged_candidate_attendee_or_title_never_saves(patch):
    bad={**proposal(),**patch}
    env=setup([QUERY,GENERATE,bad,bad])
    async with env.harness.scope(RUN):
        result=await env.workflow.build().ainvoke(initial())
    assert result["expert_result"]["status"]=="unavailable" and not env.repository.rows


@pytest.mark.asyncio
async def test_candidate_becomes_busy_before_save_returns_to_local_loop():
    env=setup([QUERY,GENERATE,proposal(),UNAVAILABLE])
    env.provider.conflict=True
    async with env.harness.scope(RUN):
        result=await env.workflow.build().ainvoke(initial())
    assert result["expert_result"]["status"]=="unavailable" and not env.repository.rows
    assert len(env.provider.calls)==2 and 'selected_time_changed' in env.client.calls[-1][1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["outside","connection","window"])
async def test_invalid_results_and_unknown_window_do_not_produce_draft(mode):
    env=setup([dict(action="query_availability",window_index=4) if mode=="window" else QUERY])
    env.provider.outside=mode=="outside"
    env.provider.active=mode!="connection"
    async with env.harness.scope(RUN):
        result=await env.workflow.build().ainvoke(initial())
    assert result["expert_result"]["status"]=="unavailable" and not env.repository.rows
    if mode in {"window","connection"}:
        assert not env.provider.calls


@pytest.mark.asyncio
async def test_empty_candidates_do_not_allow_model_to_fabricate_draft():
    env=setup([QUERY,GENERATE,UNAVAILABLE])
    env.provider.free=False
    async with env.harness.scope(RUN):
        result=await env.workflow.build().ainvoke(initial())
    assert result["expert_result"]["status"]=="unavailable" and not env.repository.rows
    assert len(env.client.calls)==3


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation",["version","content","permission"])
async def test_draft_verifier_rejects_version_content_or_permission_change(mutation):
    env=setup([QUERY,GENERATE,proposal()])
    async with env.harness.scope(RUN):
        result=await env.workflow.build().ainvoke(initial())
    output=ExpertResult.model_validate(result["expert_result"])
    if mutation=="version":
        env.repository.rows[output.draft.id]["version"]=2
    elif mutation=="content":
        env.repository.rows[output.draft.id]["title"]="未升级版本的篡改"
    else:
        env.provider.active=False
    assert not await env.drafts.verify_result(RUN,DelegationMessage.model_validate(initial()["delegation"]),output)
    with pytest.raises(ToolError):
        await env.drafts.prepare_approval(RUN,output.draft)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer",["approved","rejected"])
async def test_deterministic_approval_after_saved_checkpoint_never_calls_model_again(answer):
    env=setup([QUERY,GENERATE,proposal()])
    async with env.harness.scope(RUN):
        result=await env.workflow.build().ainvoke(initial())
    writes=[]
    class Confirmation:
        async def create_approved_event(self,run,**kwargs):
            writes.append(kwargs)
            return "event-1"
    approval=CalendarApprovalWorkflow(Confirmation(),env.drafts)
    parent=StateGraph(TaskGraphState)
    parent.add_node("approval",approval.build())
    parent.add_edge(START,"approval")
    parent.add_edge("approval",END)
    graph=parent.compile(checkpointer=MemorySaver())
    config={"configurable":{"thread_id":str(RUN.id)}}
    approval_input={**initial(),"cooperation_draft":result["expert_result"]["draft"]}
    paused=await graph.ainvoke(approval_input,config)
    payload=paused["__interrupt__"][0].value
    assert payload["approval_ready_required"] and not writes
    assert env.repository.status=="running" and not any(item["event_type"]=="approval_required" for item in env.runs.events)
    await env.drafts.mark_ready(RUN,payload)  # 实际由Orchestrator在检查点落下后执行。
    assert env.repository.status=="waiting_confirmation"
    assert any(item["event_type"]=="approval_required" for item in env.runs.events)
    completed=await graph.ainvoke(Command(resume=answer),config)
    assert len(writes)==(1 if answer=="approved" else 0) and len(env.client.calls)==3
    assert ("已创建" if answer=="approved" else "拒绝") in completed["assistant_content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer",["approved","rejected"])
async def test_real_mail_and_calendar_team_produces_draft_then_only_deterministic_approval(answer):
    from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry
    from aegis_agent_worker.agent.routing.team_registry import TeamRegistry
    from aegis_agent_worker.agent.specialists.cooperation_agent import CooperationAgent
    from aegis_agent_worker.agent.workflows.cooperation.factory import CooperationWorkflowFactory
    from test_cooperation_runtime import DELEGATE_MAIL, DELEGATE_CALENDAR, Events, state as team_state
    from test_mail_meeting_requirements import setup as mail_setup, SEARCH, read, EXTRACT, RETURN
    outputs=[DELEGATE_MAIL,SEARCH,read(),EXTRACT,RETURN,DELEGATE_CALENDAR,QUERY,GENERATE,proposal(),dict(action="finish")]
    env=setup(outputs)
    mail=mail_setup([],profile=COOPERATION_CALENDAR_POLICY,db=env.db,harness=env.harness,client=env.client)
    env.drafts._sources=mail.sources
    writes=[]
    class Confirmation:
        async def create_approved_event(self,run,**kwargs):
            writes.append(kwargs)
            return "team-event-1"
    team_events=Events()
    factory=CooperationWorkflowFactory(CooperationAgent(GuardedLLMClient(env.client,env.harness),env.policies),
        TeamRegistry((mail.workflow.binding(),env.workflow.binding())),team_events,
        CalendarApprovalWorkflow(Confirmation(),env.drafts).build(),env.policies)
    registry=GraphRegistry()
    factory.register(registry)
    parent=StateGraph(TaskGraphState)
    parent.add_node("team",registry.get("cooperation_meeting"))
    parent.add_edge(START,"team")
    parent.add_edge("team",END)
    graph=parent.compile(checkpointer=MemorySaver())
    config={"configurable":{"thread_id":str(RUN.id)}}
    async with env.harness.scope(RUN):
        paused=await graph.ainvoke(team_state(),config)
    payload=paused["__interrupt__"][0].value
    assert len(env.client.calls)==10 and env.repository.inserts==1 and not writes
    assert env.db.budgets[RUN.id]["tool_calls"]==5
    # 审批子图中断时，完整Factory尚未返回，不能假设父图已有内部State更新。
    assert payload["status"]=="waiting_confirmation"
    assert team_events.items[-1][1]["phase"]=="finished"
    await env.drafts.mark_ready(RUN,payload)
    async with env.harness.scope(RUN):
        completed=await graph.ainvoke(Command(resume=answer),config)
    assert len(env.client.calls)==10 and len(writes)==(1 if answer=="approved" else 0)
    assert ("已创建" if answer=="approved" else "拒绝") in completed["assistant_content"]


@pytest.mark.asyncio
async def test_budget_exhaustion_and_local_limit_do_not_retry_as_output_error():
    profile=replace(COOPERATION_CALENDAR_POLICY,execution=replace(COOPERATION_CALENDAR_POLICY.execution,max_models=1))
    env=setup(["invalid-json"],profile)
    async with env.harness.scope(RUN):
        with pytest.raises(HarnessError) as error:
            await env.workflow.build().ainvoke(initial())
    assert error.value.code=="EXECUTION_BUDGET_EXCEEDED" and len(env.client.calls)==1
    profile=replace(COOPERATION_CALENDAR_POLICY,calendar_team=replace(COOPERATION_CALENDAR_POLICY.calendar_team,max_rounds=1))
    env=setup([QUERY],profile)
    async with env.harness.scope(RUN):
        with pytest.raises(HarnessError) as error:
            await env.workflow.build().ainvoke(initial())
    assert error.value.code=="CALENDAR_TEAM_ROUND_LIMIT" and len(env.provider.calls)==1


@pytest.mark.asyncio
async def test_calendar_policy_gate_and_invalid_model_output_never_touch_tools():
    env=setup([],replace(COOPERATION_CALENDAR_POLICY,
        calendar_team=replace(COOPERATION_CALENDAR_POLICY.calendar_team,enabled=False)))
    async with env.harness.scope(RUN):
        with pytest.raises(HarnessError) as error:
            await env.workflow.build().ainvoke(initial())
    assert error.value.code=="CALENDAR_TEAM_POLICY_DISABLED" and not env.provider.calls
    env=setup([dict(action="calendar.create_event")]*2)
    async with env.harness.scope(RUN):
        result=await env.workflow.build().ainvoke(initial())
    assert result["expert_result"]["status"]=="unavailable" and not env.provider.calls


@pytest.mark.asyncio
async def test_final_write_known_success_is_reused_and_unknown_execution_is_not_replayed(monkeypatch):
    from unittest.mock import AsyncMock
    from aegis_agent_worker.repository.calendar_draft_repository import CalendarDraftRepository
    from aegis_agent_worker.service.calendar.calendar_confirmation_service import CalendarConfirmationService,CalendarWriteError
    import aegis_agent_worker.service.calendar.calendar_confirmation_service as module
    env=setup([])
    service=CalendarConfirmationService(env.db,env.runs,None,tools=object())
    monkeypatch.setattr(CalendarDraftRepository,"completed_execution",AsyncMock(return_value="already-created"))
    assert await service.create_approved_event(RUN,draft_id=uuid4(),approval_item_id=uuid4())=="already-created"
    monkeypatch.setattr(CalendarDraftRepository,"completed_execution",AsyncMock(return_value=None))
    monkeypatch.setattr(module,"_load_approved_draft",AsyncMock(return_value={"title":"已批准草稿"}))
    monkeypatch.setattr(CalendarDraftRepository,"has_execution",AsyncMock(return_value=True))
    with pytest.raises(CalendarWriteError,match="不自动重复写入"):
        await service.create_approved_event(RUN,draft_id=uuid4(),approval_item_id=uuid4())
    assert not env.provider.calls


@pytest.mark.asyncio
async def test_final_write_sql_requires_owner_approved_version_and_real_decision():
    from aegis_agent_worker.service.calendar.calendar_confirmation_service import _load_approved_draft
    from test_harness_react_revision import Result
    statements=[]
    class Session:
        async def execute(self,sql,args):
            statements.append((str(sql),args))
            return Result()
    assert await _load_approved_draft(Session(),run=RUN,draft_id=uuid4(),approval_item_id=uuid4()) is None
    sql,args=statements[0]
    assert all(part in sql for part in ("ai.draft_version=d.version","d.user_id=:user_id","ai.expires_at>now()",
        "ad.checked=true","ad.actor_user_id=d.user_id","'approved_executing'","FOR UPDATE"))
    assert args["user_id"]==RUN.user_id


@pytest.mark.asyncio
async def test_orchestrator_marks_ready_only_after_runner_returns_persisted_interrupt():
    from aegis_agent_worker.agent.orchestrator import AgentOrchestrator
    calls=[]
    payload=dict(approval_ready_required=True,approval_item_id=str(uuid4()),draft_id=str(uuid4()),draft_version=1)
    class Lifecycle:
        async def claim_queued_run(self,*args,**kwargs):
            return RUN
    class Runner:
        async def invoke(self,run):
            calls.append("checkpoint_saved")
            return {"__interrupt__":[SimpleNamespace(value=payload)]}
    class Readiness:
        async def mark_ready(self,run,value):
            assert value==payload
            calls.append("ready")
    env=setup([])
    orchestrator=AgentOrchestrator(env.db,env.client,lifecycle=Lifecycle(),graph_runner=Runner(),approval_readiness=Readiness())
    result=await orchestrator.execute(RUN.id)
    assert result.status=="waiting_confirmation" and calls==["checkpoint_saved","ready"]


@pytest.mark.asyncio
async def test_no_slots_can_query_another_existing_window_without_relaxing_conditions():
    data=initial()
    data["team_context"]["results"][0]["meeting_requirements"]["time_windows"].append(
        dict(start_at="2026-10-27T09:00:00+08:00",end_at="2026-10-27T17:00:00+08:00"))
    env=setup([QUERY,dict(action="query_availability",window_index=1),GENERATE,proposal("2026-10-27")])
    env.provider.free=False
    original=env.client.complete_json
    async def allow_second_window(messages):
        result=await original(messages)
        if json.loads(result).get("window_index")==1:
            env.provider.free=True
        return result
    env.client.complete_json=allow_second_window
    async with env.harness.scope(RUN):
        result=await env.workflow.build().ainvoke(data)
    assert result["expert_result"]["status"]=="completed" and not env.inputs.prepared
    assert len(env.provider.calls)==3
    assert "2026-10-27" in env.provider.calls[1][1]["start_at"]


@pytest.mark.asyncio
async def test_calendar_can_obtain_user_confirmed_conditions_without_mandatory_mail_step():
    env=setup([dict(action="ask_user",question="请确认会议条件",proposed_requirements=FACTS),QUERY,GENERATE,proposal()])
    data=initial()
    data["team_context"]["results"]=[]
    graph=root(env.workflow)
    config={"configurable":{"thread_id":str(RUN.id)}}
    async with env.harness.scope(RUN):
        first=await graph.ainvoke(data,config)
    request=first["__interrupt__"][0].value
    assert not env.provider.calls
    async with env.harness.scope(RUN):
        result=await graph.ainvoke(Command(resume=dict(question_id=request["question_id"],response="确认调整")),config)
    assert result["expert_result"]["status"]=="completed" and not result["expert_result"]["references"]
    assert result["calendar_requirements"]["user_supplemented"] is True


@pytest.mark.asyncio
async def test_calendar_network_failure_is_not_schema_repaired():
    from aegis_agent_worker.llm.client import LLMError
    env=setup([LLMError("network")])
    async with env.harness.scope(RUN):
        with pytest.raises(LLMError):
            await env.workflow.build().ainvoke(initial())
    assert len(env.client.calls)==1 and not env.provider.calls
