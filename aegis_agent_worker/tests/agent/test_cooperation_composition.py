"""任务目标约束下的子图组合；无真实模型、工具或数据库。"""
import json
from copy import deepcopy
from dataclasses import replace
import pytest
from aegis_agent_worker.agent.contracts.decision import AgentHandoff,MainDecision
from aegis_agent_worker.agent.routing.cooperation_profiles import MAIL_CALENDAR_PROFILES,CooperationGoal,CooperationRiskLimit
from aegis_agent_worker.agent.routing.cooperation_registry import CooperationRegistry,CooperationPlanError,MEETING_STEPS
from aegis_agent_worker.agent.workflows.cooperation.runtime.plan_state import verify_plan_state
from aegis_agent_worker.agent.workflows.cooperation.runtime.scheduler import CooperationScheduler
from aegis_agent_worker.harness.policies.cooperation import CooperationPlanPolicy
from aegis_agent_worker.tool.contracts import ToolError
from test_cooperation_plan import plan,registry
from test_cooperation_workflow import setup

READ_INTENT='find_free_time_from_mail'
REQUEST='请按续约邮件的时间要求查询我的空闲候选，不生成草稿。'


def read_plan():
    value=plan()
    value['steps']=value['steps'][:2]
    return value


def readonly_env(raw=None):
    env=setup()
    env.dependencies.context_service.load_history.return_value=[{'role':'user','content':REQUEST}]
    env.main_client.complete.side_effect=[json.dumps(dict(decision='delegate',domain='mail',
        intent=READ_INTENT,instruction=REQUEST)),json.dumps(raw if raw is not None else read_plan())]
    return env


def test_profiles_accept_goal_specific_subsets_not_fixed_workflow_tuple():
    catalog=registry()
    _,read=catalog.validate(read_plan(),has_verified_target=False,policy=CooperationPlanPolicy(),domain='mail',intent=READ_INTENT)
    _,meeting=catalog.validate(plan(),has_verified_target=False,policy=CooperationPlanPolicy(),domain='mail',intent='arrange_meeting_from_mail')
    assert read['step_order']==['lookup','slots']
    assert meeting['step_order']==['lookup','slots','draft']
    assert read['execution_profile']=='mail-calendar-availability-v2'
    assert meeting['execution_profile']=='mail-calendar-meeting-v4'
    assert [item['workflow'] for item in catalog.catalog(catalog.profile('mail',READ_INTENT))]==[
        'cooperation_calendar_candidates','cooperation_mail_requirements']


def test_registered_alternative_producer_is_selectable_without_scheduler_changes():
    base=registry()
    alternative=replace(MEETING_STEPS[1],workflow='calendar_candidates_alternative')
    profile=MAIL_CALENDAR_PROFILES[1]
    graphs=base.graphs()
    graphs[alternative.workflow]=graphs['cooperation_calendar_candidates']
    catalog=CooperationRegistry((*MEETING_STEPS,alternative),graphs,profiles=(profile,))
    for chosen in ('cooperation_calendar_candidates',alternative.workflow):
        raw=read_plan()
        raw['steps'][1]['workflow']=chosen
        raw['steps'].reverse()
        _,binding=catalog.validate(raw,has_verified_target=False,policy=CooperationPlanPolicy(),domain='mail',intent=READ_INTENT)
        assert binding['step_order']==['lookup','slots']
        assert len(binding['phase_steps'])==2


@pytest.mark.parametrize('case',['omit_candidates','drop_goal','add_draft','duplicate_phase','fake_workflow','unknown_profile'])
def test_composition_cannot_skip_business_inputs_or_change_goal(case):
    raw=plan()
    intent='arrange_meeting_from_mail'
    if case=='omit_candidates':
        raw['steps'].pop(1)
        raw['steps'][1]['depends_on']=['lookup']
        raw['steps'][1]['inputs']=raw['steps'][1]['inputs'][:1]
    elif case=='drop_goal': raw=read_plan()
    elif case=='add_draft': intent=READ_INTENT
    elif case=='duplicate_phase': raw['steps'].append({**deepcopy(raw['steps'][0]),'step_id':'extra'})
    elif case=='fake_workflow': raw['steps'][0]['workflow']='mail_send'
    else: intent='unsupported'
    with pytest.raises(CooperationPlanError):
        registry().validate(raw,has_verified_target=False,policy=CooperationPlanPolicy(),domain='mail',intent=intent)


def test_registration_rejects_bad_profile_and_missing_runtime_validator():
    catalog=registry()
    profiles=MAIL_CALENDAR_PROFILES
    for modified in (
        (profiles[0],profiles[0]),
        (replace(profiles[1],available_capabilities=frozenset({'unregistered'})),),
        (replace(profiles[1],goal=CooperationGoal('MeetingDraftV1',())),),
        (replace(profiles[0],risk_limit=CooperationRiskLimit()),),
    ):
        with pytest.raises(ValueError): CooperationRegistry(MEETING_STEPS,catalog.graphs(),modified)
    incomplete=CooperationRegistry(MEETING_STEPS,catalog.graphs())
    from langgraph.graph import StateGraph
    from aegis_agent_worker.agent.graph.state import TaskGraphState
    with pytest.raises(CooperationPlanError,match='result_handler_missing'):
        CooperationScheduler(lambda:incomplete,object()).register(StateGraph(TaskGraphState),incomplete.graphs())
    with pytest.raises(ValueError):
        CooperationRegistry((*MEETING_STEPS[:2],replace(MEETING_STEPS[2],risk='read')),catalog.graphs())


def test_allowed_but_unrelated_side_effect_step_is_rejected():
    base=registry()
    profile=replace(MAIL_CALENDAR_PROFILES[0],goal=CooperationGoal('CalendarCandidatesV1',('MailRequirementsV1',)))
    catalog=CooperationRegistry(MEETING_STEPS,base.graphs(),profiles=(profile,))
    with pytest.raises(CooperationPlanError,match='unrelated_step'):
        catalog.validate(plan(),has_verified_target=False,policy=CooperationPlanPolicy(),
            domain=profile.domain,intent=profile.intent)


@pytest.mark.asyncio
async def test_alternative_registered_subgraph_executes_using_unchanged_scheduler():
    from aegis_agent_worker.agent.workflows.cooperation.steps.mail.requirements_workflow import MailRequirementsWorkflow
    from aegis_agent_worker.agent.workflows.cooperation.steps.calendar.candidates_workflow import CalendarCandidatesWorkflow
    from aegis_agent_worker.agent.workflows.cooperation.factory import COOPERATION_RESULT_HANDLERS
    from aegis_agent_worker.agent.workflows.cooperation.cooperation_workflow import CooperationWorkflow
    env=readonly_env()
    factory=env.factory
    alternative=replace(MEETING_STEPS[1],workflow='calendar_candidates_alternative')
    profile=MAIL_CALENDAR_PROFILES[1]
    def provider(): return catalog
    graphs={
        MEETING_STEPS[0].workflow:MailRequirementsWorkflow(factory.mail_agent,factory.query_service,
            factory.calendar_agent,factory.source_service,provider).build(),
        alternative.workflow:CalendarCandidatesWorkflow(factory.calendar_agent,factory.confirmation_service,
            factory.source_service,provider).build(),
    }
    graphs[MEETING_STEPS[1].workflow]=graphs[alternative.workflow]
    catalog=CooperationRegistry((MEETING_STEPS[0],MEETING_STEPS[1],alternative),graphs,profiles=(profile,),
        result_handlers=COOPERATION_RESULT_HANDLERS)
    raw=read_plan()
    raw['steps'][1]['workflow']=alternative.workflow
    env.main_client.complete.side_effect=[json.dumps(raw)]
    state={**env.state,'handoff':AgentHandoff(domain='mail',intent=READ_INTENT,instruction=REQUEST).model_dump(mode='json'),
           'latest_user_message':REQUEST}
    done=await CooperationWorkflow(factory.planner,factory.source_service,provider).build().ainvoke(state)
    assert done['cooperation_summary']['status']=='completed'
    assert done['cooperation_summary']['steps'][1]['workflow']==alternative.workflow
    assert '未创建草稿或写入日历' in done['assistant_content']
    env.confirmation.query_available_slots.assert_awaited_once()
    env.confirmation.create_recommended_draft.assert_not_awaited()
    env.confirmation.create_approved_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_goal_routes_plans_executes_two_steps_and_never_creates_draft():
    env=readonly_env()
    done=await env.graph.ainvoke(env.state,env.config)
    assert done['main_decision']['intent']==READ_INTENT
    assert done['workflow']=='mail_calendar_meeting'
    assert done['cooperation_status']=='completed' and not done.get('__interrupt__')
    assert set(done['cooperation_results'])=={'mail_requirements','calendar_candidates'}
    assert '未创建草稿或写入日历' in done['assistant_content']
    assert '2026-10-26 10:00' in done['assistant_content']
    env.confirmation.create_recommended_draft.assert_not_awaited()
    env.confirmation.create_approved_event.assert_not_awaited()
    assert env.confirmation.query_available_slots.await_count==1
    assert env.calendar_client.complete.await_count==1  # 只有时间事实规划，无草稿模型调用
    assert env.source.record_scheduled_step.await_count==2
    prompt=str(env.main_client.complete.await_args)
    assert 'mail-calendar-availability-v2' in prompt
    assert 'cooperation_meeting_draft' not in prompt
    verify_plan_state(done,registry)
    tampered=deepcopy(done)
    tampered['handoff']['intent']='arrange_meeting_from_mail'
    with pytest.raises(CooperationPlanError,match='required_goal_missing'):
        verify_plan_state(tampered,registry)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['escalate','skip_candidate','downgrade_goal'])
async def test_invalid_composition_has_zero_business_calls(mode):
    raw=read_plan() if mode=='downgrade_goal' else plan()
    env=readonly_env(raw)
    if mode=='skip_candidate':
        raw['steps'].pop(1)
        raw['steps'][1]['depends_on']=['lookup']
        raw['steps'][1]['inputs']=raw['steps'][1]['inputs'][:1]
    intent='arrange_meeting_from_mail' if mode!='escalate' else READ_INTENT
    env.main_client.complete.side_effect=[json.dumps(dict(decision='delegate',domain='mail',
        intent=intent,instruction=REQUEST)),json.dumps(raw)]
    done=await env.graph.ainvoke(env.state,env.config)
    assert done['cooperation_status']=='unavailable' and done['cooperation_clarification']
    env.query.query.assert_not_awaited()
    env.source.read_source.assert_not_awaited()
    env.confirmation.query_available_slots.assert_not_awaited()
    env.confirmation.create_recommended_draft.assert_not_awaited()
    env.confirmation.create_approved_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_revocation_does_not_present_candidates_as_success():
    env=readonly_env()
    env.source.verify_source.side_effect=ToolError('COOP_SOURCE_CHANGED','revoked')
    done=await env.graph.ainvoke(env.state,env.config)
    assert done['cooperation_summary']['status']=='unavailable'
    assert '2026-10-26 10:00' not in done['assistant_content']
    env.confirmation.create_approved_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_revocation_at_final_delivery_prevents_completed_summary():
    env=readonly_env()
    calls=0
    async def record(run,metadata,result):
        nonlocal calls
        calls+=1
        if calls==2: env.source.verify_source.side_effect=ToolError('COOP_SOURCE_CHANGED','revoked')
        return metadata
    env.source.record_scheduled_step.side_effect=record
    with pytest.raises(ToolError): await env.graph.ainvoke(env.state,env.config)
    env.source.record_summary.assert_not_awaited()
    assert not any(call.kwargs.get('stage')=='finished' and call.kwargs.get('status')=='completed'
                   for call in env.source.record_progress.await_args_list)
    env.confirmation.create_approved_event.assert_not_awaited()


def test_new_intent_is_mail_only_and_preserves_source_requirements():
    MainDecision(decision='delegate',domain='mail',intent=READ_INTENT,instruction=REQUEST)
    with pytest.raises(ValueError):
        MainDecision(decision='delegate',domain='calendar',intent=READ_INTENT,instruction=REQUEST)
    assert AgentHandoff(domain='mail',intent=READ_INTENT,instruction=REQUEST).intent==READ_INTENT
