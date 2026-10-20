"""唯一协同链路的配置、注册、旧任务拒绝和恢复门禁。"""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import pytest
from pydantic import ValidationError
from langgraph.graph import StateGraph
from test_cooperation_workflow import setup,REQUEST
from test_cooperation_plan import registry,nested_state
from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.routing.cooperation_registry import CooperationPlanError
from aegis_agent_worker.agent.workflows.cooperation.runtime.plan_state import verify_plan_state
from aegis_agent_worker.agent.workflows.cooperation.planning_workflow import CooperationPlanningWorkflow
from aegis_agent_worker.agent.workflows.cooperation.runtime.scheduler import CooperationScheduler
from aegis_agent_worker.config.settings import Settings
from aegis_agent_worker.harness.policy_provider import DEFAULT_POLICIES,COOPERATION_POLICY,COOPERATION_KNOWLEDGE_POLICY,LEGACY_POLICY
from aegis_agent_worker.harness.exceptions import HarnessError

SETTINGS=dict(_env_file=None,database_url='postgresql://unused',redis_url='redis://localhost:6379',
    model_provider='deepseek',model_api_base='https://model.example.com/v1',model_api_key='test',model_default_name='test')
RETIRED_FLAGS=('FIXED_COOPERATION_ENABLED','COOPERATION_PLANNING_ENABLED','COOPERATION_SCHEDULING_ENABLED')
RETIRED_POLICIES=('harness-v3-fixed-cooperation','harness-v3-cooperation-knowledge',
    'harness-v4-cooperation-plan','harness-v4-cooperation-plan-knowledge',
    'harness-v5-cooperation-schedule','harness-v5-cooperation-schedule-knowledge')


def test_single_switch_is_off_by_default_and_has_no_mode_options():
    assert not Settings(**SETTINGS).cooperation_enabled
    assert Settings(**SETTINGS,cooperation_enabled=True).cooperation_enabled
    assert not any(name.lower() in Settings.model_fields for name in RETIRED_FLAGS)
    env=setup()
    assert not hasattr(env.factory,'plan_enabled') and not hasattr(env.factory,'schedule_enabled')


@pytest.mark.parametrize('key',RETIRED_FLAGS)
def test_retired_options_are_rejected_in_arguments_environment_and_dotenv(key,monkeypatch,tmp_path):
    with pytest.raises(ValidationError,match='COOPERATION_ENABLED'):
        Settings(**SETTINGS,**{key.lower():False})
    monkeypatch.setenv(key,'false')
    with pytest.raises(ValidationError,match='COOPERATION_ENABLED'): Settings(**SETTINGS)
    monkeypatch.delenv(key)
    path=tmp_path/'retired.env'
    path.write_text(key+'=false',encoding='utf-8')
    with pytest.raises(ValidationError,match='COOPERATION_ENABLED'):
        Settings(**{**SETTINGS,'_env_file':path})


def test_root_registers_only_public_entry_and_internal_graphs_remain_private():
    env=setup()
    root=GraphRegistry()
    env.factory.register(root)
    assert root.names()=={'mail_calendar_meeting'}
    public=set(root.get('mail_calendar_meeting').get_graph().nodes)
    assert {'choose_next','verify_result','plan_cooperation','cooperation_mail_requirements',
            'cooperation_calendar_candidates','cooperation_meeting_draft'}<=public
    assert not {'find_source_mail','extract_and_handoff','calendar_meeting','finish_fixed_cooperation'} & public
    assert not any(name.startswith('cooperation_') for name in env.graph.get_graph().nodes)
    assert not hasattr(root,'cooperation_capabilities')


def test_dispatch_requires_same_compiled_graph_objects_as_catalog():
    catalog=registry()
    graphs=catalog.graphs()
    graphs['cooperation_mail_requirements']=object()
    with pytest.raises(CooperationPlanError,match='step_graph_binding_mismatch'):
        CooperationScheduler(lambda:catalog,object()).register(StateGraph(TaskGraphState),graphs)


@pytest.mark.parametrize('version',RETIRED_POLICIES)
@pytest.mark.asyncio
async def test_retired_budget_cannot_be_upgraded_or_spent(version):
    from test_harness_layers import Database,Repository
    from aegis_agent_worker.harness.executor import HarnessExecutor
    from aegis_agent_worker.repository.run_repository import ClaimedRun
    repo=Repository()
    repo.ensure_budget=AsyncMock(return_value=version)
    harness=HarnessExecutor(Database(),repository=repo,policy_provider=DEFAULT_POLICIES)
    run=ClaimedRun(id=UUID(int=1),tenant_id=UUID(int=2),user_id=UUID(int=3),conversation_id=UUID(int=4))
    with pytest.raises(HarnessError) as error:
        async with harness.scope(run): pytest.fail('retired task must not execute')
    assert error.value.code=='POLICY_VERSION_UNAVAILABLE'
    assert not repo.reservations and not repo.started
    assert repo.ensure_budget.await_count==1


@pytest.mark.asyncio
async def test_missing_plan_or_retired_engine_cannot_bypass_planning():
    env=setup()
    await env.graph.ainvoke(env.state,env.config)
    state=dict(nested_state(env))
    for marker in (None,'cooperation-scheduler-v1','cooperation-scheduler-v2','cooperation-scheduler-v3'):
        bad={**state,'cooperation_scheduler_version':marker}
        with pytest.raises(CooperationPlanError,match='scheduler_version_mismatch'):
            verify_plan_state(bad,registry)
        planner=SimpleNamespace(plan=AsyncMock())
        with pytest.raises(CooperationPlanError,match='scheduler_version_mismatch'):
            await CooperationPlanningWorkflow(planner,registry,env.source).build().ainvoke(bad)
        planner.plan.assert_not_awaited()
    bad={**state,'cooperation_plan':{},'cooperation_plan_binding':{}}
    with pytest.raises(CooperationPlanError,match='missing_validated_plan'): verify_plan_state(bad,registry)


def test_policy_and_scope_are_explicit_and_do_not_expand_old_permissions():
    assert COOPERATION_POLICY.version=='harness-v6-cooperation'
    assert COOPERATION_POLICY.cooperation.version=='cooperation-v2'
    assert COOPERATION_POLICY.execution.max_tools==8 and COOPERATION_POLICY.execution.max_models==10
    assert 'mail.messages.get' not in LEGACY_POLICY.execution.tools('conversation')
    assert 'mail.messages.get' in COOPERATION_POLICY.execution.tools('conversation')
    assert 'mail.messages.send' not in COOPERATION_POLICY.execution.tools('conversation')
    assert 'knowledge.search' in COOPERATION_KNOWLEDGE_POLICY.execution.tools('conversation')
