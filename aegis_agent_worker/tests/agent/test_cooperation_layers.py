"""分层依赖、迁移前图拓扑及重建图后的审批恢复。"""
import ast
from dataclasses import replace
from pathlib import Path
import pytest
from langgraph.types import Command
from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry
from aegis_agent_worker.agent.graph.task_graph import TaskGraph
from aegis_agent_worker.agent.contracts.decision import AgentHandoff
from aegis_agent_worker.agent.routing.cooperation_registry import CooperationRegistry,MEETING_STEPS
from aegis_agent_worker.agent.workflows.cooperation.runtime.execution import guarded_step
from aegis_agent_worker.agent.workflows.cooperation.runtime.plan_state import ENGINE_VERSION
from aegis_agent_worker.agent.workflows.cooperation.runtime.results import require_succeeded_result
from aegis_agent_worker.harness.policy_provider import COOPERATION_POLICY
from aegis_agent_worker.llm.client import LLMError
from test_cooperation_plan import plan,registry
from test_cooperation_workflow import setup,REQUEST


def test_runtime_has_no_domain_implementation_or_steps_imports():
    import aegis_agent_worker.agent.workflows.cooperation.runtime as runtime
    package='aegis_agent_worker.agent.workflows.cooperation.runtime'
    for path in Path(runtime.__file__).parent.glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node,ast.Import): names=[item.name for item in node.names]
            elif isinstance(node,ast.ImportFrom):
                if node.level:
                    base=package.split('.')[:len(package.split('.'))-node.level+1]
                    module='.'.join([*base,node.module] if node.module else base)
                else: module=node.module or ''
                names=[module]
            else: continue
            for name in names:
                assert 'workflows.cooperation.steps' not in name,path
                assert not any(part in name for part in ('workflows.calendar','workflows.mail','specialists.','service.','repository.cooperation')),path


def test_flat_compatibility_modules_are_not_left_behind():
    import aegis_agent_worker.agent.workflows.cooperation as cooperation
    folder=Path(cooperation.__file__).parent
    assert {item.name for item in folder.glob('*.py')}=={
        '__init__.py','factory.py','cooperation_workflow.py','planning_workflow.py'}
    assert (folder/'steps/mail/requirements_workflow.py').is_file()
    assert (folder/'steps/calendar/meeting_confirmation_workflow.py').is_file()


def test_compiled_topology_matches_pre_move_nodes_and_edges():
    env=setup()
    root=GraphRegistry()
    env.factory.register(root)
    graph=root.get('mail_calendar_meeting')
    edges={
        ('__start__','validate_cooperation_entry',False),
        ('validate_cooperation_entry','begin_cooperation_schedule',False),
        ('begin_cooperation_schedule','begin_cooperation',False),
        ('begin_cooperation','plan_cooperation',False),
        ('plan_cooperation','__end__',True),('plan_cooperation','validate_schedule',True),
        ('validate_schedule','choose_next',False),('choose_next','finish_cooperation',True),
        ('verify_result','choose_next',False),('finish_cooperation','__end__',False),
    }
    for name in ('cooperation_mail_requirements','cooperation_calendar_candidates','cooperation_meeting_draft'):
        edges|={('choose_next',name,True),(name,'verify_result',False)}
    actual=graph.get_graph()
    assert {(edge.source,edge.target,bool(edge.conditional)) for edge in actual.edges}==edges
    assert set(actual.nodes)=={node for edge in edges for node in edge[:2]}
    expected={
        'cooperation_mail_requirements':{('__start__','find_source_mail',False),
            ('find_source_mail','__end__',True),('find_source_mail','extract_and_handoff',True),
            ('extract_and_handoff','__end__',False)},
        'cooperation_calendar_candidates':{('__start__','prepare',False),('prepare','query',False),('query','__end__',False)},
        'cooperation_meeting_draft':{('__start__','generate',False),('generate','finish',True),
            ('generate','save',True),('save','wait',False),('wait','finish',False),('finish','__end__',False)},
        'plan_cooperation':{('__start__','__end__',True),('__start__','generate_and_validate_plan',True),
            ('generate_and_validate_plan','record_plan_audit',False),('record_plan_audit','__end__',False)},
    }
    children=dict(graph.get_subgraphs(recurse=True))
    assert children.keys()==expected.keys()
    for name,child in children.items():
        actual=child.get_graph()
        assert {(edge.source,edge.target,bool(edge.conditional)) for edge in actual.edges}==expected[name]
        assert set(actual.nodes)=={node for edge in expected[name] for node in edge[:2]}


@pytest.mark.asyncio
async def test_runtime_uses_registered_failure_adapter_not_domain_branch():
    from aegis_agent_worker.agent.workflows.cooperation.factory import COOPERATION_RESULT_HANDLERS
    handlers=dict(COOPERATION_RESULT_HANDLERS)
    handlers['MailRequirementsV1']=replace(handlers['MailRequirementsV1'],failure_issue='requirements_unclear',
        failure_updates=(('confirmation_error','来自注册的领域适配'),),failure_content='来自注册的失败文案')
    base=registry()
    catalog=CooperationRegistry(MEETING_STEPS,base.graphs(),result_handlers=handlers)
    raw=plan()
    _,binding=catalog.validate(raw,has_verified_target=False,policy=COOPERATION_POLICY.cooperation,
        domain='mail',intent='arrange_meeting_from_mail')
    state={'execution_policy_version':COOPERATION_POLICY.version,'cooperation_scheduler_version':ENGINE_VERSION,
        'handoff':AgentHandoff(domain='mail',intent='arrange_meeting_from_mail',instruction=REQUEST).model_dump(mode='json'),
        'cooperation_plan':raw,'cooperation_plan_binding':binding,'cooperation_current_step':'lookup',
        'cooperation_progress':{key:{'status':'pending','result_ref':None} for key in binding['step_order']}}
    async def fail(state): raise LLMError('offline')
    result=await guarded_step(fail,'mail_requirements',lambda:catalog)(state)
    assert result['cooperation_failure_code']=='LLM_UNAVAILABLE'
    assert result['cooperation_error']=='requirements_unclear'
    assert result['assistant_content']=='来自注册的失败文案'
    assert result['confirmation_error']=='来自注册的领域适配'


@pytest.mark.asyncio
@pytest.mark.parametrize('decision',['approved','rejected'])
async def test_approval_checkpoint_resumes_after_graph_rebuild(decision):
    env=setup()
    assert (await env.graph.ainvoke(env.state,env.config))['__interrupt__']
    counts=(env.main_client.complete.await_count,env.mail_client.complete.await_count,env.calendar_client.complete.await_count)
    rebuilt=TaskGraph(env.dependencies).build(checkpointer=env.checkpointer)
    done=await rebuilt.ainvoke(Command(resume=decision),env.config)
    assert done['cooperation_status']==('completed' if decision=='approved' else 'rejected')
    assert done['cooperation_scheduler_version']==ENGINE_VERSION=='cooperation-scheduler-v4'
    assert counts==(env.main_client.complete.await_count,env.mail_client.complete.await_count,env.calendar_client.complete.await_count)
    env.confirmation.create_recommended_draft.assert_awaited_once()
    assert env.confirmation.create_approved_event.await_count==(decision=='approved')


def test_shared_result_reader_rejects_wrong_phase_or_unfinished_result():
    with pytest.raises(ValueError):
        require_succeeded_result({'cooperation_results':{'calendar_candidates':{
            'step_id':'mail_requirements','status':'unavailable','issue_code':'mail_not_found'}}},'calendar_candidates')
