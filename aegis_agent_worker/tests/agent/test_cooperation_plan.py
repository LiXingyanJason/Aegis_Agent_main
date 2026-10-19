"""8.2静态目录、DAG验证、模型有界修复和同run状态/恢复测试；无外部服务。"""
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID
from unittest.mock import AsyncMock
import pytest
from langgraph.types import Command
from pydantic import ValidationError
from aegis_agent_worker.agent.contracts.cooperation_plan import CooperationPlan
from aegis_agent_worker.agent.contracts.decision import AgentHandoff,ConversationCapability
from aegis_agent_worker.agent.routing.cooperation_registry import CooperationRegistry,CooperationPlanError,FIXED_MEETING_STEPS
from aegis_agent_worker.agent.workflows.cooperation.plan_state import verify_plan_state,update_plan_progress
from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry
from aegis_agent_worker.agent.graph.task_graph import TaskGraph
from aegis_agent_worker.harness.policies.cooperation import CooperationPlanPolicy
from aegis_agent_worker.harness.policy_provider import COOPERATION_PLAN_POLICY,COOPERATION_POLICY,PolicyProvider,LEGACY_POLICY
from aegis_agent_worker.agent.specialists.main_agent import MainAgent
from aegis_agent_worker.agent.specialists.cooperation_planner import CooperationPlanner
from aegis_agent_worker.llm.client import LLMError
from test_fixed_cooperation import setup,Client,REQUEST,SOURCE,PLAN


def plan():
    return {'version':1,'steps':[
        {'step_id':'lookup','workflow':'cooperation_mail_requirements','depends_on':[],
            'inputs':[{'kind':'task','name':'user_request'}],'expected_output':'MailRequirementsV1'},
        {'step_id':'slots','workflow':'cooperation_calendar_candidates','depends_on':['lookup'],
            'inputs':[{'kind':'step','step_id':'lookup','output_contract':'MailRequirementsV1'}],'expected_output':'CalendarCandidatesV1'},
        {'step_id':'draft','workflow':'cooperation_meeting_draft','depends_on':['lookup','slots'],
            'inputs':[{'kind':'step','step_id':'lookup','output_contract':'MailRequirementsV1'},
                      {'kind':'step','step_id':'slots','output_contract':'CalendarCandidatesV1'}],'expected_output':'MeetingDraftV1'},
    ]}


def registry():
    return CooperationRegistry(FIXED_MEETING_STEPS,{'mail_calendar_meeting','calendar_read',*(item.workflow for item in FIXED_MEETING_STEPS)},
        (ConversationCapability(domain='mail',intent='arrange_meeting_from_mail',workflow='mail_calendar_meeting',description='fixed'),))


def validate(data,*,target=False,policy=None):
    return registry().validate(data,has_verified_target=target,policy=policy or CooperationPlanPolicy())


def nested_state(env):
    snapshot=env.graph.get_state(env.config,subgraphs=True)
    while snapshot.tasks and getattr(snapshot.tasks[0].state,'values',None) is not None:
        snapshot=snapshot.tasks[0].state
    return snapshot.values


def test_registered_plan_has_deterministic_order_and_symbolic_inputs_only():
    raw=plan()
    value,binding=validate(raw)
    assert binding['step_order']==['lookup','slots','draft']
    assert binding['phase_steps']=={'mail_requirements':'lookup','calendar_candidates':'slots','meeting_draft':'draft'}
    assert validate(deepcopy(raw))[1]==binding
    raw['steps'].reverse()
    assert validate(raw)[1]['step_order']==binding['step_order']
    assert registry().digest==registry().digest
    with pytest.raises(ValidationError): CooperationPlan.model_validate({**plan(),'tenant_id':str(UUID(int=2))})
    raw=plan();raw['steps'][0]['inputs'].append({'kind':'task','name':'verified_target'})
    assert validate(raw,target=True)[0]


BAD_CASES=[
    ('duplicate_id',lambda p:p['steps'][1].update(step_id='lookup')),
    ('unknown_workflow',lambda p:p['steps'][0].update(workflow='mail_messages_send')),
    ('root_but_not_step',lambda p:p['steps'][0].update(workflow='calendar_read')),
    ('missing_dependency',lambda p:p['steps'][1].update(depends_on=['missing'])),
    ('self_dependency',lambda p:p['steps'][0].update(depends_on=['lookup'])),
    ('cycle',lambda p:p['steps'][0].update(depends_on=['draft'])),
    ('duplicate_dependency',lambda p:p['steps'][1].update(depends_on=['lookup','lookup'])),
    ('wrong_output',lambda p:p['steps'][0].update(expected_output='MeetingDraftV1')),
    ('input_producer_missing',lambda p:p['steps'][1]['inputs'][0].update(step_id='missing')),
    ('wrong_input_contract',lambda p:p['steps'][1]['inputs'][0].update(output_contract='MeetingDraftV1')),
    ('undeclared_dependency',lambda p:p['steps'][1].update(depends_on=[])),
    ('unused_dependency',lambda p:p['steps'][2]['inputs'].pop(0)),
    ('forged_target',lambda p:p['steps'][0]['inputs'].append({'kind':'task','name':'verified_target'})),
    ('forged_id',lambda p:p['steps'][0]['inputs'][0].update(id=str(UUID(int=99)))),
    ('forged_tenant',lambda p:p['steps'][0].update(tenant_id=str(UUID(int=99)))),
    ('duplicate_input',lambda p:p['steps'][0]['inputs'].append({'kind':'task','name':'user_request'})),
    ('wrong_task_port',lambda p:p['steps'][1]['inputs'].append({'kind':'task','name':'user_request'})),
    ('too_many_steps',lambda p:p['steps'].extend([deepcopy(p['steps'][0]) for _ in range(4)])),
]


@pytest.mark.parametrize('name,mutate',BAD_CASES,ids=[case[0] for case in BAD_CASES])
def test_invalid_dag_registry_or_references_are_rejected(name,mutate):
    raw=plan();mutate(raw)
    with pytest.raises(CooperationPlanError): validate(raw)


def test_complete_registered_chain_and_policy_limits_are_required():
    raw=plan();raw['steps'].append({**deepcopy(raw['steps'][0]),'step_id':'extra'})
    with pytest.raises(CooperationPlanError,match='unsupported_execution_profile'): validate(raw)
    for policy in (CooperationPlanPolicy(max_steps=2),CooperationPlanPolicy(max_plan_bytes=10),CooperationPlanPolicy(max_dependencies=1)):
        with pytest.raises(CooperationPlanError): validate(plan(),policy=policy)
    with pytest.raises(CooperationPlanError,match='target_reference_required'): validate(plan(),target=True)
    with pytest.raises(ValueError): CooperationRegistry(FIXED_MEETING_STEPS,{'mail_calendar_meeting'},())
    with pytest.raises(ValueError): GraphRegistry().register_cooperation_capability(FIXED_MEETING_STEPS[0])


@pytest.mark.asyncio
async def test_root_plan_is_checkpointed_with_progress_and_same_run_approval_resume():
    env=setup(plan_enabled=True,plan_response=json.dumps(plan()))
    paused=await env.graph.ainvoke(env.state,env.config)
    assert paused['__interrupt__']
    state=nested_state(env)
    assert state['cooperation_plan']==CooperationPlan.model_validate(plan()).model_dump(mode='json')
    assert state['cooperation_current_step']=='draft' and state['cooperation_status']=='waiting_confirmation'
    assert state['cooperation_progress']['lookup']=={'status':'succeeded','result_ref':'cooperation_results.mail_requirements'}
    assert state['cooperation_progress']['slots']['status']=='succeeded'
    assert state['cooperation_progress']['draft']['status']=='waiting_confirmation'
    calls=env.main_client.complete.await_count
    done=await env.graph.ainvoke(Command(resume='approved'),env.config)
    assert done['cooperation_status']=='completed' and done['cooperation_current_step']==''
    assert done['cooperation_progress']['draft']['status']=='completed'
    assert env.main_client.complete.await_count==calls==2
    env.confirmation.create_approved_event.assert_awaited_once()
    assert done['run_id']==env.state['run_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('name,mutate',BAD_CASES,ids=[case[0] for case in BAD_CASES])
async def test_invalid_plan_never_starts_mail_or_calendar_tools(name,mutate):
    raw=plan();mutate(raw)
    env=setup(plan_enabled=True,plan_response=json.dumps(raw))
    # Schema错误修复也返回相同非法计划，不能因重试静默执行默认步骤。
    env.main_client.complete.side_effect=[json.dumps({'decision':'delegate','domain':'mail',
        'intent':'arrange_meeting_from_mail','instruction':REQUEST}),json.dumps(raw),json.dumps(raw)]
    done=await env.graph.ainvoke(env.state,env.config)
    assert done['workflow']=='mail_calendar_meeting' and done['assistant_content']
    assert done['main_decision']['decision']=='delegate' and done['handoff']['intent']=='arrange_meeting_from_mail'
    assert done['cooperation_clarification']['code']==done['cooperation_error']
    routing=env.dependencies.decision_context_service.record_decision.await_args.kwargs
    assert routing['outcome']=='validated' and 'cooperation_plan_audit' not in routing
    env.source.record_plan.assert_awaited_once()
    assert done['cooperation_status']=='unavailable' and not done['cooperation_plan']
    env.query.query.assert_not_awaited();env.source.read_source.assert_not_awaited()
    env.confirmation.query_available_slots.assert_not_awaited();env.confirmation.create_recommended_draft.assert_not_awaited()
    env.confirmation.create_approved_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_v3_checkpoint_can_resume_after_plan_feature_enabled():
    from test_harness_layers import Database,Repository
    from aegis_agent_worker.harness.executor import HarnessExecutor
    from aegis_agent_worker.repository.run_repository import ClaimedRun
    repository=Repository()
    repository.ensure_budget=AsyncMock(return_value=COOPERATION_POLICY.version)
    policies=PolicyProvider((LEGACY_POLICY,COOPERATION_POLICY,COOPERATION_PLAN_POLICY),default_version=COOPERATION_PLAN_POLICY.version)
    harness=HarnessExecutor(Database(),repository=repository,policy_provider=policies)
    run=ClaimedRun(id=UUID(int=1),tenant_id=UUID(int=2),user_id=UUID(int=3),conversation_id=UUID(int=4))
    env=setup()
    async with harness.scope(run):
        assert (await env.graph.ainvoke(env.state,env.config))['__interrupt__']
    assert 'cooperation_plan' not in nested_state(env)
    assert nested_state(env)['execution_policy_version']==COOPERATION_POLICY.version
    new_factory=replace(env.factory,plan_enabled=True,planner=CooperationPlanner(env.main_client))
    env.graph=TaskGraph(replace(env.dependencies,workflow_factories=(new_factory,))).build(checkpointer=env.checkpointer)
    async with harness.scope(run):
        done=await env.graph.ainvoke(Command(resume='rejected'),env.config)
    assert done['cooperation_results']['meeting_draft']['status']=='rejected'
    assert done['execution_policy_version']==COOPERATION_POLICY.version
    assert env.main_client.complete.await_count==1
    env.confirmation.create_approved_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_domain_or_reply_does_not_generate_cooperation_plan():
    env=setup(plan_enabled=True)
    env.main_client.complete.side_effect=[json.dumps({'decision':'reply'})]
    reply=Client('你好')
    env.graph=TaskGraph(replace(env.dependencies,llm_client=reply,
        lifecycle=SimpleNamespace(start_llm_generation=AsyncMock(return_value=UUID(int=10))))).build(checkpointer=env.checkpointer)
    done=await env.graph.ainvoke(env.state,env.config)
    assert done['assistant_content']=='你好' and not done.get('cooperation_plan')
    assert env.main_client.complete.await_count==1
    env.query.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_page_direct_task_bypasses_main_and_plan():
    env=setup(plan_enabled=True)
    class PageFactory:
        def register(self,registry):
            async def page(_state): return {'assistant_content':'页面明确任务已处理'}
            registry.register('mail_extraction',page)
    env.graph=TaskGraph(replace(env.dependencies,workflow_factories=(env.factory,PageFactory()))).build(checkpointer=env.checkpointer)
    done=await env.graph.ainvoke({**env.state,'run_type':'mail_extraction'},env.config)
    assert done['assistant_content']=='页面明确任务已处理' and not done.get('cooperation_plan')
    env.main_client.complete.assert_not_awaited()


def test_planning_flag_requires_fixed_cooperation():
    from aegis_agent_worker.config.settings import Settings
    values=dict(_env_file=None,database_url='postgresql://unused',redis_url='redis://localhost:6379',
        model_provider='deepseek',model_api_base='https://model.example.com/v1',model_api_key='test-key',model_default_name='test')
    with pytest.raises(ValidationError): Settings(**values,cooperation_planning_enabled=True,fixed_cooperation_enabled=False)
    assert Settings(**values,cooperation_planning_enabled=True,fixed_cooperation_enabled=True).cooperation_planning_enabled


@pytest.mark.asyncio
async def test_binding_tamper_rejected_before_source_or_write_and_results_have_quotas(monkeypatch):
    env=setup(plan_enabled=True,plan_response=json.dumps(plan()))
    await env.graph.ainvoke(env.state,env.config)
    state=nested_state(env)
    provider=registry
    verify_plan_state(state,provider,required=True)
    modified=deepcopy(state);modified['cooperation_plan_binding']['catalog_digest']='0'*64
    with pytest.raises(CooperationPlanError,match='plan_binding_changed'): verify_plan_state(modified,provider)
    from aegis_agent_worker.agent.contracts.cooperation import TaskStepResult
    result=TaskStepResult.model_validate(state['cooperation_results']['mail_requirements'])
    # 输出标签绑定真实Schema：邮件步骤不能夹带已批准草稿引用。
    forged=result.model_copy(update={'draft_id':UUID(int=9)})
    with pytest.raises(CooperationPlanError,match='step_result_contract_mismatch'): update_plan_progress(state,forged,provider)
    from aegis_agent_worker.agent.workflows.cooperation.fixed_meeting_workflow import CooperationCalendarWorkflow
    consumer=CooperationCalendarWorkflow(None,env.confirmation,env.source,plan_registry=provider)
    env.confirmation.query_available_slots.reset_mock()
    bad_dependency=deepcopy(state);bad_dependency['cooperation_progress']['lookup']['status']='unavailable'
    with pytest.raises(CooperationPlanError,match='dependency_result_not_ready'):
        await consumer._query_availability(bad_dependency)
    env.confirmation.query_available_slots.assert_not_awaited()
    from aegis_agent_worker.agent.workflows.cooperation import plan_state
    limits=replace(LEGACY_POLICY,cooperation=CooperationPlanPolicy(max_step_result_bytes=10))
    monkeypatch.setattr(plan_state,'DEFAULT_POLICIES',PolicyProvider((limits,)))
    with pytest.raises(CooperationPlanError,match='step_result_bytes_exceeded'): update_plan_progress(state,result,provider)


@pytest.mark.asyncio
async def test_planning_repairs_share_guarded_budget_and_never_echo_invalid_output():
    from test_harness_layers import Database,Repository
    from aegis_agent_worker.harness.executor import HarnessExecutor
    from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
    from aegis_agent_worker.repository.run_repository import ClaimedRun
    repository=Repository()
    harness=HarnessExecutor(Database(),repository=repository,policy_provider=PolicyProvider((COOPERATION_PLAN_POLICY,),default_version=COOPERATION_PLAN_POLICY.version))
    client=Client('SECRET_INVALID_MODEL_OUTPUT',json.dumps(plan()))
    agent=CooperationPlanner(GuardedLLMClient(client,harness))
    run=ClaimedRun(id=UUID(int=1),tenant_id=UUID(int=2),user_id=UUID(int=3),conversation_id=UUID(int=4))
    async with harness.scope(run):
        value,repairs=await agent.plan(message=REQUEST,handoff=AgentHandoff(domain='mail',intent='arrange_meeting_from_mail',instruction=REQUEST),catalog=registry().catalog())
    assert len(value.steps)==3 and repairs==1 and len(repository.reservations)==2
    assert [row['purpose'] for row in repository.finished]==['cooperation_planning','cooperation_planning_repair']
    assert all(row['memory_count']==0 for row in repository.finished)
    assert 'SECRET_INVALID_MODEL_OUTPUT' not in str(client.complete.await_args)


@pytest.mark.asyncio
async def test_network_and_budget_errors_do_not_trigger_format_retry():
    from aegis_agent_worker.harness.exceptions import HarnessError
    for error in (LLMError('offline'),HarnessError('EXECUTION_BUDGET_EXCEEDED','budget')):
        client=Client();client.complete.side_effect=error
        with pytest.raises(type(error)):
            await CooperationPlanner(client).plan(message=REQUEST,
                handoff=AgentHandoff(domain='mail',intent='arrange_meeting_from_mail',instruction=REQUEST),catalog=registry().catalog())
        assert client.complete.await_count==1


@pytest.mark.asyncio
async def test_plan_audit_only_publishes_whitelisted_metadata():
    from test_mail_query_workflow import Database
    from aegis_agent_worker.service.cooperation.fixed_meeting_service import FixedMeetingService
    from aegis_agent_worker.repository.run_repository import ClaimedRun
    from aegis_agent_worker.agent.contracts.decision import MainDecision
    runs=SimpleNamespace(append_event=AsyncMock())
    service=FixedMeetingService(Database(),runs,None)
    run=ClaimedRun(id=UUID(int=1),tenant_id=UUID(int=2),user_id=UUID(int=3),conversation_id=UUID(int=4))
    await service.record_plan(run, {
            'outcome':'validated','step_count':3,'plan_digest':'a'*64,'full_prompt':'PRIVATE_PROMPT','raw_response':'PRIVATE_OUTPUT'})
    payload=runs.append_event.await_args.kwargs['payload']
    assert payload=={'outcome':'validated','step_count':3,'plan_digest':'a'*64}
    assert runs.append_event.await_args.kwargs['event_type']=='cooperation_plan_completed'
    assert 'PRIVATE_' not in json.dumps(payload)


@pytest.mark.asyncio
async def test_planning_occurs_after_main_audit_and_inside_domain_workflow():
    env=setup(plan_enabled=True,plan_response=json.dumps(plan()))
    original=env.factory.planner.plan
    async def plan_after_handoff(**kwargs):
        audit=env.dependencies.decision_context_service.record_decision
        audit.assert_awaited_once()
        assert audit.await_args.kwargs['outcome']=='validated'
        env.query.query.assert_not_awaited()
        return await original(**kwargs)
    env.factory.planner.plan=AsyncMock(side_effect=plan_after_handoff)
    assert (await env.graph.ainvoke(env.state,env.config))['__interrupt__']
    env.source.record_plan.assert_awaited_once()
    assert env.source.record_plan.await_args.args[1]['outcome']=='validated'
    env.source.record_plan.reset_mock()
    await env.graph.ainvoke(Command(resume='rejected'),env.config)
    env.factory.planner.plan.assert_awaited_once()
    env.source.record_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_valid_plan_is_reused_without_replanning_or_resetting_progress():
    from aegis_agent_worker.agent.workflows.cooperation.planning_workflow import CooperationPlanningWorkflow
    env=setup(plan_enabled=True,plan_response=json.dumps(plan()))
    await env.graph.ainvoke(env.state,env.config)
    state=nested_state(env)
    planner=SimpleNamespace(plan=AsyncMock(side_effect=AssertionError('must not replan')))
    audit=SimpleNamespace(record_plan=AsyncMock())
    value=await CooperationPlanningWorkflow(planner,registry,audit).build().ainvoke(state)
    assert value['cooperation_plan_binding']==state['cooperation_plan_binding']
    assert value['cooperation_progress']==state['cooperation_progress']
    planner.plan.assert_not_awaited();audit.record_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_domain_planning_network_failure_preserves_main_decision_and_blocks_tools():
    env=setup(plan_enabled=True)
    env.factory.planner.plan=AsyncMock(side_effect=LLMError('offline'))
    with pytest.raises(LLMError): await env.graph.ainvoke(env.state,env.config)
    env.dependencies.decision_context_service.record_decision.assert_awaited_once()
    assert env.dependencies.decision_context_service.record_decision.await_args.kwargs['outcome']=='validated'
    assert env.source.record_plan.await_args.args[1]['outcome']=='model_failed'
    env.query.query.assert_not_awaited();env.confirmation.query_available_slots.assert_not_awaited()
    env.confirmation.create_recommended_draft.assert_not_awaited()


def test_main_decision_graph_has_no_domain_planner_or_workflow_special_case():
    from aegis_agent_worker.agent.workflows.main.decision_workflow import MainDecisionWorkflow
    main=SimpleNamespace(decide=AsyncMock())
    graph=MainDecisionWorkflow(main,SimpleNamespace(record_decision=AsyncMock()),()).build()
    assert 'plan_cooperation' not in graph.get_graph().nodes
    assert not hasattr(MainAgent,'plan_cooperation')


def test_enabled_factory_requires_explicit_planner():
    env=setup()
    with pytest.raises(ValueError,match='CooperationPlanner'):
        replace(env.factory,plan_enabled=True).register(GraphRegistry())
