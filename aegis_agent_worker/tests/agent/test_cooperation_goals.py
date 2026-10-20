"""目标/风险契约、确定性直达与真实可选计划分支；全部离线。"""
import json
from copy import deepcopy
from dataclasses import fields,replace
from unittest.mock import AsyncMock
import pytest
from langgraph.types import Command
from aegis_agent_worker.agent.graph.task_graph import TaskGraph
from aegis_agent_worker.agent.routing.cooperation_profiles import CooperationRiskLimit,MAIL_CALENDAR_PROFILES
from aegis_agent_worker.agent.routing.cooperation_registry import CooperationRegistry,CooperationPlanError,MEETING_STEPS
from aegis_agent_worker.agent.workflows.cooperation.factory import CooperationWorkflowFactory,COOPERATION_RESULT_HANDLERS
from aegis_agent_worker.agent.workflows.cooperation.runtime.scheduler import CooperationScheduler
from aegis_agent_worker.harness.policy_provider import COOPERATION_POLICY
from aegis_agent_worker.llm.client import LLMError
from aegis_agent_worker.tool.contracts import ToolError
from test_cooperation_workflow import setup,REQUEST
from test_cooperation_composition import read_plan,READ_INTENT,readonly_env
from test_cooperation_plan import plan,registry,nested_state


class AlternativeFactory(CooperationWorkflowFactory):
    def build_registry(self,steps):
        alternative=replace(MEETING_STEPS[1],workflow='calendar_alternative')
        graphs={**steps,alternative.workflow:steps[MEETING_STEPS[1].workflow]}
        return CooperationRegistry((*MEETING_STEPS,alternative),graphs,result_handlers=COOPERATION_RESULT_HANDLERS)


class OptionalFactory(CooperationWorkflowFactory):
    def build_registry(self,steps):
        draft=replace(MEETING_STEPS[2],input_contracts=('CalendarCandidatesV1',),
            optional_input_contracts=('MailRequirementsV1',))
        return CooperationRegistry((*MEETING_STEPS[:2],draft),steps,result_handlers=COOPERATION_RESULT_HANDLERS)


def production_env(factory_type=CooperationWorkflowFactory,*,readonly=False):
    env=readonly_env() if readonly else setup()
    factory=factory_type(**{field.name:getattr(env.factory,field.name) for field in fields(CooperationWorkflowFactory)})
    env.factory=factory
    env.dependencies=replace(env.dependencies,workflow_factories=(factory,))
    env.graph=TaskGraph(env.dependencies).build(checkpointer=env.checkpointer)
    return env


def test_profile_declares_goal_risk_and_logical_capabilities_not_workflow_sequence():
    profile=MAIL_CALENDAR_PROFILES[0]
    description=profile.describe()
    assert not hasattr(profile,'allowed_workflows')
    assert description['goal']=={'output_contract':'MeetingDraftV1',
        'required_evidence':['MailRequirementsV1','CalendarCandidatesV1'],'missing_information':'clarify'}
    assert description['risk_limit']=={'max_effect':'approved_external_write','external_write':'approval_only','approval_grant':False}
    assert all(not name.startswith('cooperation_') for name in description['available_capabilities'])
    assert not CooperationRiskLimit('internal_draft').permits('approved_external_write')
    assert not CooperationRiskLimit('approved_external_write','forbidden').permits('approved_external_write')


@pytest.mark.parametrize('kwargs',[{'approval_grant':True},{'approval_grant':None},
    {'max_effect':'unrestricted'},{'external_write':'allow'}, {'max_effect':'read','external_write':'approval_only'}])
def test_risk_limit_cannot_create_authority(kwargs):
    with pytest.raises(ValueError): CooperationRiskLimit(**kwargs)


def test_risk_filters_catalog_and_is_checked_again_for_model_plan():
    base=registry()
    profile=replace(MAIL_CALENDAR_PROFILES[1],available_capabilities=MAIL_CALENDAR_PROFILES[0].available_capabilities)
    catalog=CooperationRegistry(MEETING_STEPS,base.graphs(),profiles=(profile,))
    assert 'cooperation_meeting_draft' not in {row['workflow'] for row in catalog.catalog(profile)}
    with pytest.raises(CooperationPlanError,match='step_risk_exceeded'):
        catalog.validate(plan(),has_verified_target=False,policy=COOPERATION_POLICY.cooperation,domain='mail',intent=READ_INTENT)


def test_unique_resolution_has_symbolic_inputs_and_still_requires_goal_validation():
    catalog=registry()
    raw=catalog.unique_plan(domain='mail',intent='arrange_meeting_from_mail',has_verified_target=True,
        policy=COOPERATION_POLICY.cooperation)
    assert [step.workflow for step in raw.steps]==[item.workflow for item in MEETING_STEPS]
    assert any(item.kind=='task' and item.name=='verified_target' for item in raw.steps[0].inputs)
    _,binding=catalog.validate(raw,has_verified_target=True,policy=COOPERATION_POLICY.cooperation,
        domain='mail',intent='arrange_meeting_from_mail')
    assert binding['risk_limit']['approval_grant'] is False and binding['goal_digest']
    with pytest.raises(CooperationPlanError,match='unverified_target_reference'):
        catalog.validate(raw,has_verified_target=False,policy=COOPERATION_POLICY.cooperation,
            domain='mail',intent='arrange_meeting_from_mail')
    with pytest.raises(CooperationPlanError,match='direct_plan_limit_exceeded'):
        catalog.unique_plan(domain='mail',intent='arrange_meeting_from_mail',has_verified_target=False,
            policy=replace(COOPERATION_POLICY.cooperation,max_steps=2))


@pytest.mark.asyncio
@pytest.mark.parametrize('readonly',[True,False])
async def test_unique_production_path_does_not_call_planning_model(readonly):
    env=production_env(readonly=readonly)
    env.factory.planner.plan=AsyncMock(side_effect=AssertionError('路径唯一不得调用规划模型'))
    done=await env.graph.ainvoke(env.state,env.config)
    state=done if readonly else nested_state(env)
    assert state['cooperation_plan_audit']['planning_strategy']=='deterministic'
    assert state['cooperation_plan_audit']['repair_count']==0
    assert env.main_client.complete.await_count==1  # 主路由仍使用LLM
    env.factory.planner.plan.assert_not_awaited()
    assert env.source.record_plan.await_args.args[1]['planning_strategy']=='deterministic'
    if readonly:
        assert done['cooperation_summary']['goal_output']=='CalendarCandidatesV1'
        assert len(done['cooperation_summary']['goal_checks'])==2
        env.confirmation.create_recommended_draft.assert_not_awaited()
    else:
        assert done['__interrupt__']
        done=await env.graph.ainvoke(Command(resume='approved'),env.config)
        assert done['cooperation_status']=='completed' and len(done['cooperation_summary']['goal_checks'])==3
        env.confirmation.create_approved_event.assert_awaited_once()
        env.factory.planner.plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_alternative_registered_producers_use_agent_planning_without_profile_workflow_edits():
    env=production_env(AlternativeFactory,readonly=True)
    raw=read_plan()
    raw['steps'][1]['workflow']='calendar_alternative'
    env.main_client.complete.side_effect=[json.dumps(dict(decision='delegate',domain='mail',intent=READ_INTENT,
        instruction='按续约邮件查询空闲，不生成草稿')),json.dumps(raw)]
    done=await env.graph.ainvoke(env.state,env.config)
    assert done['cooperation_plan_audit']['planning_strategy']=='agent'
    assert done['cooperation_plan']['steps'][1]['workflow']=='calendar_alternative'
    assert done['cooperation_status']=='completed' and env.main_client.complete.await_count==2
    env.confirmation.create_recommended_draft.assert_not_awaited()
    env.confirmation.create_approved_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('include_optional',[False,True])
async def test_agent_may_choose_optional_input_edges_but_not_drop_required_evidence(include_optional):
    env=production_env(OptionalFactory)
    raw=plan()
    if not include_optional:
        raw['steps'][2]['depends_on']=['slots']
        raw['steps'][2]['inputs']=raw['steps'][2]['inputs'][1:]
    env.main_client.complete.side_effect=[json.dumps(dict(decision='delegate',domain='mail',
        intent='arrange_meeting_from_mail',instruction=REQUEST)),json.dumps(raw)]
    assert (await env.graph.ainvoke(env.state,env.config))['__interrupt__']
    state=nested_state(env)
    assert state['cooperation_plan_audit']['planning_strategy']=='agent'
    assert len(state['cooperation_plan']['steps'][2]['inputs'])==(2 if include_optional else 1)
    done=await env.graph.ainvoke(Command(resume='rejected'),env.config)
    assert done['cooperation_status']=='rejected'
    env.confirmation.create_approved_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_network_failure_does_not_fall_back_to_direct_execution():
    env=production_env(AlternativeFactory,readonly=True)
    env.factory.planner.plan=AsyncMock(side_effect=LLMError('offline'))
    with pytest.raises(LLMError): await env.graph.ainvoke(env.state,env.config)
    env.factory.planner.plan.assert_awaited_once()
    env.query.query.assert_not_awaited()
    env.confirmation.query_available_slots.assert_not_awaited()


def test_goal_evidence_cannot_be_removed_by_a_more_permissive_producer():
    base=registry()
    # 即使某个代码注册生产者不要求邮件输入，任务目标仍要求可信邮件事实。
    candidates=replace(MEETING_STEPS[1],input_contracts=('user_request',),task_inputs=('user_request',))
    catalog=CooperationRegistry((MEETING_STEPS[0],candidates),{
        name:graph for name,graph in base.graphs().items() if name!='cooperation_meeting_draft'},
        profiles=(MAIL_CALENDAR_PROFILES[1],))
    raw={'version':1,'steps':[dict(step_id='slots',workflow=candidates.workflow,depends_on=[],
        inputs=[dict(kind='task',name='user_request')],expected_output='CalendarCandidatesV1')]}
    with pytest.raises(CooperationPlanError,match='goal_evidence_missing'):
        catalog.validate(raw,has_verified_target=False,policy=COOPERATION_POLICY.cooperation,domain='mail',intent=READ_INTENT)


@pytest.mark.asyncio
async def test_progress_flags_are_not_goal_evidence_and_completed_is_not_published():
    env=production_env(readonly=True)
    done=await env.graph.ainvoke(env.state,env.config)
    damaged=deepcopy(done)
    damaged['cooperation_results'].pop('mail_requirements')
    env.source.record_summary.reset_mock()
    env.source.record_progress.reset_mock()
    with pytest.raises(CooperationPlanError,match='goal_evidence_invalid'):
        await CooperationScheduler(registry,env.source)._finish(damaged)
    env.source.record_summary.assert_not_awaited()
    env.source.record_progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_goal_revalidation_revocation_does_not_regenerate_or_write_again():
    env=production_env(readonly=True)
    done=await env.graph.ainvoke(env.state,env.config)
    env.source.verify_source.side_effect=ToolError('COOP_SOURCE_CHANGED','revoked')
    env.source.record_summary.reset_mock()
    with pytest.raises(ToolError): await CooperationScheduler(registry,env.source)._finish(done)
    env.source.record_summary.assert_not_awaited()
    env.confirmation.create_approved_event.assert_not_awaited()


def test_old_profile_and_risk_fields_cannot_be_supplied_by_model():
    from aegis_agent_worker.agent.contracts.cooperation_plan import CooperationPlan
    for key in ('goal','risk_limit','available_capabilities','approved'):
        with pytest.raises(ValueError): CooperationPlan.model_validate({**plan(),key:True})
