"""8.4生产装配/根图/双专家/中断恢复；SQL、提供商和模型用替身。"""
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import MemorySaver

from aegis_agent_worker.agent.contracts.cooperation.context import TeamContext
from aegis_agent_worker.agent.contracts.cooperation.messages import DelegationMessage, ExpertResult
from aegis_agent_worker.agent.contracts.input_choice import choice_id
from aegis_agent_worker.agent.orchestrator import AgentOrchestrator
from aegis_agent_worker.agent.specialists.main_agent import MainAgent
from aegis_agent_worker.agent.workflows.cooperation.context import expert_input
from aegis_agent_worker.agent.workflows.cooperation.bootstrap import create_cooperation_runtime
from aegis_agent_worker.harness.bootstrap import create_worker_policy_provider
from aegis_agent_worker.harness.policy_provider import (
    LEGACY_POLICY, KNOWLEDGE_CONTRACT_POLICY, COOPERATION_CALENDAR_POLICY, COOPERATION_KNOWLEDGE_POLICY,
)
from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
from test_calendar_team_expert import setup as calendar_setup, QUERY, GENERATE, proposal
from test_mail_meeting_requirements import setup as mail_setup, SEARCH, read, EXTRACT, RETURN, FACTS, reference, BODY
from test_cooperation_runtime import RUN, DELEGATE_MAIL, DELEGATE_CALENDAR, state, setup as team_setup


MAIN = dict(decision="delegate", domain="cooperation", intent="mail_calendar_meeting",
            instruction="请按续约邮件中的时间要求查询日历并安排会议草稿")


@pytest.mark.parametrize("knowledge,team,expected",[
    (False,False,LEGACY_POLICY),(True,False,KNOWLEDGE_CONTRACT_POLICY),
    (False,True,COOPERATION_CALENDAR_POLICY),(True,True,COOPERATION_KNOWLEDGE_POLICY),
])
def test_production_policy_combinations_preserve_scopes_and_old_snapshots(knowledge,team,expected):
    policies=create_worker_policy_provider(knowledge_enabled=knowledge,cooperation_enabled=team)
    assert policies.current()==expected
    assert ("knowledge.search" in policies.current().execution.tools("conversation"))==knowledge
    assert ("mail.messages.get" in policies.current().execution.tools("conversation"))==team
    assert "mail.messages.send" not in policies.current().execution.tools("conversation")
    assert policies.resolve(LEGACY_POLICY.version)==LEGACY_POLICY
    assert policies.resolve(KNOWLEDGE_CONTRACT_POLICY.version)==KNOWLEDGE_CONTRACT_POLICY
    assert policies.resolve(COOPERATION_CALENDAR_POLICY.version)==COOPERATION_CALENDAR_POLICY


def test_expert_projection_keeps_only_latest_relevant_slices_not_full_history():
    results=[ExpertResult(delegation_id=UUID(int=index),expert="mail",status="completed",summary=f"反馈{index}",
        references=[reference()],meeting_requirements=FACTS) for index in (1,2)]
    results.append(ExpertResult(delegation_id=UUID(int=3),expert="calendar",status="unavailable",
        summary="该范围无空闲，请邮件专家核对其他窗口",error_code="NO_AVAILABILITY"))
    context=TeamContext(goal=MAIN["instruction"],results=results)
    payload=state()|dict(execution_policy_version=COOPERATION_CALENDAR_POLICY.version,credentials="secret")
    for target in ("mail","calendar"):
        delegation=DelegationMessage(delegation_id=UUID(int=4),sequence=4,target=target,goal="继续核对")
        actual=expert_input(payload,context,delegation)
        assert set(actual)=={"run_id","tenant_id","user_id","conversation_id","run_type",
                             "execution_policy_version","delegation","team_context"}
        sliced=TeamContext.model_validate(actual["team_context"])
        assert len(sliced.results)==2 and sliced.results[0].summary=="反馈2"
        assert (sliced.results[0].meeting_requirements is not None)==(target=="calendar")
        assert sliced.results[1].error_code=="NO_AVAILABILITY"
    assert len(context.results)==3  # 投影不改审计历史，也不修改原始结果。


class Lifecycle:
    def __init__(self):
        self.completed=[]
        self.failed=[]
    async def claim_queued_run(self,*args,**kwargs): return RUN
    async def resume_waiting_confirmation_run(self,*args,**kwargs): return RUN
    async def complete_run(self,run,**kwargs): self.completed.append(kwargs)
    async def fail_run(self,run,**kwargs): self.failed.append(kwargs)


def assemble(monkeypatch,outputs,*,count=1,context_tools=None):
    """生产装配函数不替换，服务使用真实代码，仅替换各自的SQL Repository。"""
    import aegis_agent_worker.agent.workflows.cooperation.bootstrap as module
    from aegis_agent_worker.service.mail.mail_meeting_source_service import MailMeetingSourceService
    from aegis_agent_worker.service.calendar.calendar_draft_service import CalendarDraftService
    env=calendar_setup(outputs)
    mail=mail_setup([],count=count,profile=COOPERATION_CALENDAR_POLICY,
        db=env.db,harness=env.harness,client=env.client)
    def source_service(db,queries,reader):
        assert db is env.db and queries is mail.sources._queries and reader is mail.sources._reader
        return MailMeetingSourceService(db,queries,reader,repository=mail.store)
    def draft_service(db,runs,sources,events):
        return CalendarDraftService(db,runs,sources,events,repository=env.repository)
    monkeypatch.setattr(module,"MailMeetingSourceService",source_service)
    monkeypatch.setattr(module,"CalendarDraftService",draft_service)
    writes=[]
    class Confirmation:
        async def create_approved_event(self,run,**kwargs):
            writes.append(kwargs)
            return "real-saved-event"
    guarded=GuardedLLMClient(env.client,env.harness)
    runtime=create_cooperation_runtime(database=env.db,runs=env.runs,events=None,llm_client=guarded,
        mail_agent=mail.agent,calendar_agent=env.workflow._agent,mail_queries=mail.sources._queries,
        mail_reader=mail.sources._reader,calendar_queries=env.queries._tools,
        calendar_confirmation=Confirmation(),input_requests=mail.inputs,memory_context=None,policy_provider=env.policies)
    audit=[]
    class Audit:
        async def record_decision(self,run,**kwargs): audit.append(kwargs)
    class Context:
        async def load_history(self,run,context):
            return [dict(role="user",content=MAIN["instruction"])]
    ready=[]
    input_metadata=[]
    answers={}
    async def mark_ready(run,question_id,**kwargs):
        ready.append(question_id)
        input_metadata.append(kwargs)
    async def claim(run_id,question_id):
        value=answers[question_id]
        return RUN,dict(question_id=str(question_id),**(value if isinstance(value,dict) else dict(response=value)))
    mail.inputs.mark_ready=mark_ready
    mail.inputs.claim=claim
    lifecycle=Lifecycle()
    orchestrator=AgentOrchestrator(env.db,guarded,lifecycle=lifecycle,context_service=Context(),
        workflow_factories=(runtime.factory,),checkpointer=MemorySaver(),
        main_agent=MainAgent(guarded,policy_provider=env.policies),decision_context_service=Audit(),
        harness=env.harness,input_requests=mail.inputs,approval_readiness=runtime.approval_readiness,
        main_context_tools=context_tools)
    return SimpleNamespace(env=env,mail=mail,runtime=runtime,orchestrator=orchestrator,writes=writes,
                           audit=audit,ready=ready,answers=answers,lifecycle=lifecycle,input_metadata=input_metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision",["approved","rejected"])
async def test_production_assembly_root_routes_once_and_resumes_each_original_expert(monkeypatch,decision):
    outputs=[MAIN,DELEGATE_MAIL,SEARCH,read(),read(),EXTRACT,RETURN,DELEGATE_CALENDAR,
        dict(action="ask_user",question="是否仍按原会议要求继续？"),QUERY,GENERATE,proposal(),dict(action="finish")]
    app=assemble(monkeypatch,outputs,count=2)
    result=await app.orchestrator.execute(RUN.id)
    assert result.status=="waiting_input" and len(app.env.client.calls)==4
    mail_question=app.ready[-1]
    before=deepcopy(app.env.db.budgets[RUN.id])
    app.answers[mail_question]=dict(selected_option_id=choice_id(mail_question,reference()))
    result=await app.orchestrator.execute(RUN.id,resume_input_id=mail_question)
    assert result.status=="waiting_input" and len(app.env.client.calls)==9
    calendar_question=app.ready[-1]
    assert calendar_question!=mail_question and len(app.audit)==1
    app.answers[calendar_question]="按原要求继续"
    result=await app.orchestrator.execute(RUN.id,resume_input_id=calendar_question)
    assert result.status=="waiting_confirmation", app.lifecycle.failed
    assert app.env.repository.inserts==1 and not app.writes and not app.lifecycle.completed
    assert app.env.repository.status=="waiting_confirmation"  # 就绪发生在Checkpoint之后。
    assert len(app.env.client.calls)==13 and len(app.audit)==1
    assert app.env.db.budgets[RUN.id]["model_calls"]==13>before["model_calls"]
    assert app.env.db.budgets[RUN.id]["tool_calls"]==5
    result=await app.orchestrator.execute(RUN.id,resume_decision=decision)
    assert result.status=="completed", app.lifecycle.failed
    assert len(app.env.client.calls)==13 and len(app.writes)==(decision=="approved")
    assert app.env.repository.inserts==1 and len(app.audit)==1
    delegated=[item["payload"] for item in app.env.runs.events
               if item["event_type"]=="cooperation_progress" and item["payload"]["phase"]=="delegated"]
    assert [(item["agent_id"],item["sequence"]) for item in delegated]==[("mail",1),("calendar",2)]
    assert [item["cooperation"]["input_kind"] for item in app.input_metadata]==["mail_selection","calendar_requirements"]
    resumed_events=[item["payload"] for item in app.env.runs.events
        if item["event_type"]=="cooperation_progress" and item["payload"]["phase"]=="resumed"]
    assert [item["cooperation"]["actor"] for item in resumed_events]==["mail","calendar"]
    assert [item["question_id"] for item in resumed_events]==[str(mail_question),str(calendar_question)]
    # 原文只能出现在邮件提取调用；协调、日历模型及根图结果均不广播原文。
    for messages in app.env.client.calls:
        if BODY in "".join(item.content for item in messages):
            assert "日历" not in messages[0].content and "协调" not in messages[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("available",[True,False])
async def test_same_request_can_finish_or_stop_based_on_actual_expert_feedback(monkeypatch,available):
    tail=[GENERATE,proposal(),dict(action="finish")] if available else [dict(action="finish_unavailable")]*2
    app=assemble(monkeypatch,[MAIN,DELEGATE_MAIL,SEARCH,read(),EXTRACT,RETURN,DELEGATE_CALENDAR,QUERY,*tail])
    app.env.provider.free=available
    result=await app.orchestrator.execute(RUN.id)
    assert result.status==("waiting_confirmation" if available else "completed"), app.lifecycle.failed
    assert app.env.repository.inserts==int(available) and not app.writes
    if not available:
        assert not app.env.repository.approvals
        assert "尚未生成" in app.lifecycle.completed[-1]["assistant_content"]


@pytest.mark.asyncio
async def test_mail_question_resume_does_not_reuse_revoked_candidate_or_reroute(monkeypatch):
    app=assemble(monkeypatch,[MAIN,DELEGATE_MAIL,SEARCH,read(),dict(action="finish_unavailable")],count=2)
    assert (await app.orchestrator.execute(RUN.id)).status=="waiting_input"
    app.mail.store.active=False
    question=app.ready[-1]
    app.answers[question]=dict(selected_option_id=choice_id(question,reference()))
    result=await app.orchestrator.execute(RUN.id,resume_input_id=question)
    assert result.status=="completed" and len(app.audit)==1 and len(app.env.client.calls)==5
    assert len(app.mail.store.tool_calls)==1 and not app.env.provider.calls
    # 恢复后唯一模型调用是协调结束；不再向邮件模型发送旧标签/正文。
    assert "续约会议1" not in "".join(item.content for item in app.env.client.calls[-1])


@pytest.mark.asyncio
async def test_calendar_failure_can_redelegate_mail_using_verified_reference_not_fixed_topology(monkeypatch):
    app=assemble(monkeypatch,[MAIN,DELEGATE_MAIL,SEARCH,read(),EXTRACT,RETURN,DELEGATE_CALENDAR,QUERY,
        dict(action="finish_unavailable"),dict(action="delegate",expert="mail",goal="核对原邮件是否有其他允许时间"),
        read(),EXTRACT,RETURN,dict(action="finish_unavailable")])
    app.env.provider.free=False
    result=await app.orchestrator.execute(RUN.id)
    assert result.status=="completed", app.lifecycle.failed
    delegated=[item["payload"]["agent_id"] for item in app.env.runs.events
               if item["event_type"]=="cooperation_progress" and item["payload"]["phase"]=="delegated"]
    assert delegated==["mail","calendar","mail"]
    assert sum(arguments["__tool_name"]=="mail.messages.list" for _,arguments in app.mail.store.tool_calls)==1
    assert not app.env.repository.rows and not app.writes and len(app.env.client.calls)==14


@pytest.mark.asyncio
async def test_main_card_read_can_still_enter_team_and_query_its_own_verified_source(monkeypatch):
    from test_main_card_react import Tools, READ
    tools=Tools()
    app=assemble(monkeypatch,[READ,MAIN,DELEGATE_MAIL,SEARCH,read(),EXTRACT,RETURN,
        DELEGATE_CALENDAR,QUERY,GENERATE,proposal(),dict(action="finish")],context_tools=tools)
    result=await app.orchestrator.execute(RUN.id)
    assert result.status=="waiting_confirmation", app.lifecycle.failed
    assert len(tools.calls)==1 and app.audit[0]["context_read_count"]==1
    assert app.audit[0]["decision"].domain=="cooperation"
    assert app.env.repository.inserts==1 and not app.writes
    # 卡片的email_id=9不应成为团队可信来源；来源来自专家自己的真实授权查询。
    metadata=next(iter(app.env.repository.rows.values()))["metadata"]
    assert metadata["source_references"][0]["id"]==reference()["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change",["source_revoked","draft_changed","calendar_revoked"])
async def test_approval_resume_rechecks_sources_and_saved_version_without_model_or_write(monkeypatch,change):
    app=assemble(monkeypatch,[MAIN,DELEGATE_MAIL,SEARCH,read(),EXTRACT,RETURN,
        DELEGATE_CALENDAR,QUERY,GENERATE,proposal(),dict(action="finish")])
    assert (await app.orchestrator.execute(RUN.id)).status=="waiting_confirmation"
    calls=len(app.env.client.calls)
    if change=="source_revoked": app.mail.store.active=False
    elif change=="draft_changed": next(iter(app.env.repository.rows.values()))["version"]+=1
    else: app.env.provider.active=False
    result=await app.orchestrator.execute(RUN.id,resume_decision="approved")
    assert result.status=="failed" and not app.writes
    assert len(app.env.client.calls)==calls and app.env.repository.inserts==1


@pytest.mark.asyncio
async def test_source_revocation_while_calendar_waits_stops_before_more_model_calls(monkeypatch):
    app=assemble(monkeypatch,[MAIN,DELEGATE_MAIL,SEARCH,read(),EXTRACT,RETURN,DELEGATE_CALENDAR,
        dict(action="ask_user",question="请确认是否按原要求继续？")])
    assert (await app.orchestrator.execute(RUN.id)).status=="waiting_input"
    calls=len(app.env.client.calls)
    app.mail.store.active=False
    question=app.ready[-1]
    app.answers[question]="继续"
    result=await app.orchestrator.execute(RUN.id,resume_input_id=question)
    assert result.status=="completed" and not app.writes and not app.env.repository.rows
    assert len(app.env.client.calls)==calls and len(app.audit)==1
    assert "尚未生成" in app.lifecycle.completed[-1]["assistant_content"]


@pytest.mark.asyncio
async def test_old_draft_cannot_hide_newer_unresolved_expert_result():
    graph,harness,client,db,events,mail,calendar=team_setup([
        DELEGATE_CALENDAR,DELEGATE_MAIL,dict(action="finish")])
    async with harness.scope(RUN): result=await graph.build().ainvoke(state())
    assert result["cooperation_error_code"]=="COOPERATION_MISSING_DRAFT"
    assert not result["cooperation_draft"]


@pytest.mark.asyncio
async def test_old_budget_cannot_gain_team_permissions_from_new_default(monkeypatch):
    app=assemble(monkeypatch,[MAIN])
    from aegis_agent_worker.harness.executor import HarnessExecutor
    from aegis_agent_worker.harness.policy_provider import PolicyProvider
    old=HarnessExecutor(app.env.db,policy_provider=PolicyProvider())
    async with old.scope(RUN): pass
    result=await app.orchestrator.execute(RUN.id)
    assert result.status=="failed" and result.error_code=="COOPERATION_POLICY_DISABLED"
    assert app.env.db.budgets[RUN.id]["policy_version"]==LEGACY_POLICY.version
    assert len(app.env.client.calls)==1 and not app.mail.store.tool_calls and not app.env.provider.calls
