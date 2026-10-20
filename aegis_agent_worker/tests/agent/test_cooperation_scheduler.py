"""8.3真实静态子图调度与中断恢复；外部服务使用替身。"""
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from langgraph.types import Command
from test_cooperation_plan import plan,registry,nested_state
from test_cooperation_workflow import setup
from aegis_agent_worker.agent.graph.task_graph import TaskGraph
from aegis_agent_worker.agent.workflows.cooperation.runtime.scheduler import CooperationScheduler
from aegis_agent_worker.agent.routing.cooperation_registry import CooperationPlanError
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.harness.policies.cooperation import CooperationPlanPolicy
from aegis_agent_worker.llm.client import LLMError
from aegis_agent_worker.tool.contracts import ToolError


def scheduled():
    return setup(plan_response=json.dumps(plan()))


@pytest.mark.asyncio
@pytest.mark.parametrize('decision',['approved','rejected'])
async def test_schedule_dispatches_verified_steps_then_resumes_without_reexecution(decision):
    env=scheduled()
    paused=await env.graph.ainvoke(env.state,env.config)
    assert paused['__interrupt__']
    state=nested_state(env)
    assert state['cooperation_scheduler_version']=='cooperation-scheduler-v4'
    assert set(state['cooperation_results'])=={'mail_requirements','calendar_candidates'}
    assert state['cooperation_pending_result']['status']=='waiting_confirmation'
    assert state['cooperation_progress']['draft']['status']=='waiting_confirmation'
    assert env.source.record_scheduled_step.await_count==2
    calls=(env.main_client.complete.await_count,env.mail_client.complete.await_count,env.calendar_client.complete.await_count)
    done=await env.graph.ainvoke(Command(resume=decision),env.config)
    assert done['cooperation_summary']['status']==('completed' if decision=='approved' else 'rejected')
    assert done['cooperation_summary']['failed_steps']==[]
    assert env.source.record_scheduled_step.await_count==3
    assert [call.args[1]['step_id'] for call in env.source.record_scheduled_step.await_args_list]==['lookup','slots','draft']
    assert calls==(env.main_client.complete.await_count,env.mail_client.complete.await_count,env.calendar_client.complete.await_count)
    assert env.query.query.await_count==1 and env.confirmation.query_available_slots.await_count==1
    assert env.confirmation.create_recommended_draft.await_count==1
    assert env.confirmation.create_approved_event.await_count==(decision=='approved')
    # 再次resume不能重做已结束节点或写入。
    await env.graph.ainvoke(Command(resume=decision),env.config)
    assert env.confirmation.create_approved_event.await_count==(decision=='approved')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['missing','ambiguous','mail_timeout','mail_model','calendar_empty','calendar_tool','draft_invalid'])
async def test_step_failures_keep_partial_results_and_block_dependents(mode):
    env=scheduled()
    if mode=='missing': env.query.query.return_value=([],'mock')
    if mode=='ambiguous': env.query.query.return_value=([{'id':1},{'id':2}],'mock')
    if mode=='mail_timeout': env.query.query.side_effect=HarnessError('EXECUTION_TIMEOUT','timeout')
    if mode=='mail_model': env.mail_client.complete.side_effect=LLMError('offline')
    if mode=='calendar_empty': env.confirmation.query_available_slots.return_value={'confirmation_error':'no slots'}
    if mode=='calendar_tool': env.confirmation.query_available_slots.side_effect=ToolError('MCP_UNAVAILABLE','offline')
    if mode=='draft_invalid': env.calendar_client.complete.side_effect=[json.dumps(__import__('test_cooperation_workflow').PLAN),'{}','{}']
    done=await env.graph.ainvoke(env.state,env.config)
    assert not done.get('__interrupt__')
    summary=done['cooperation_summary']
    assert summary['status']=='unavailable' and len(summary['failed_steps'])==1
    failed=summary['failed_steps'][0]
    assert failed==('lookup' if mode in {'missing','ambiguous','mail_timeout','mail_model'} else 'draft' if mode=='draft_invalid' else 'slots')
    if failed=='lookup':
        assert summary['blocked_steps']==['slots','draft']
        env.confirmation.query_available_slots.assert_not_awaited()
    elif failed=='slots': assert summary['completed_steps']==['lookup'] and summary['blocked_steps']==['draft']
    else: assert summary['completed_steps']==['lookup','slots']
    env.confirmation.create_recommended_draft.assert_not_awaited()
    env.confirmation.create_approved_event.assert_not_awaited()
    assert '未全部完成' in done['assistant_content']


@pytest.mark.asyncio
async def test_cancelled_before_first_step_stops_tools():
    env=scheduled()
    env.source.check_execution.side_effect=HarnessError('EXECUTION_CANCELLED','cancelled')
    with pytest.raises(HarnessError,match='cancelled'):
        await env.graph.ainvoke(env.state,env.config)
    env.query.query.assert_not_awaited();env.source.read_source.assert_not_awaited()
    env.confirmation.create_approved_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_or_revoked_source_at_approval_resume_prevents_write():
    for mode in ('cancel','source'):
        env=scheduled()
        await env.graph.ainvoke(env.state,env.config)
        if mode=='cancel': env.source.check_execution.side_effect=HarnessError('EXECUTION_CANCELLED','cancelled')
        else: env.source.verify_source.side_effect=ToolError('COOP_SOURCE_CHANGED','revoked')
        if mode=='cancel':
            with pytest.raises(HarnessError): await env.graph.ainvoke(Command(resume='approved'),env.config)
        else:
            done=await env.graph.ainvoke(Command(resume='approved'),env.config)
            assert done['cooperation_summary']['status']=='unavailable'
        env.confirmation.create_approved_event.assert_not_awaited()


def test_depth_and_parallelism_are_bounded():
    with pytest.raises(CooperationPlanError,match='plan_depth_exceeded'):
        registry().validate(plan(),has_verified_target=False,policy=CooperationPlanPolicy(max_depth=2),
            domain='mail',intent='arrange_meeting_from_mail')
    with pytest.raises(ValueError): CooperationPlanPolicy(max_parallel=2)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['calendar_candidates','mail_requirements'])
async def test_staged_source_forgery_is_not_committed(phase):
    env=scheduled()
    await env.graph.ainvoke(env.state,env.config)
    state=nested_state(env)
    # 构造候选步骤返回后、verify_result前的状态；伪造来源必须降为不可用。
    state=deepcopy(state);state['cooperation_current_step']='slots' if phase=='calendar_candidates' else 'lookup'
    raw=state['cooperation_results'].pop(phase)
    if phase=='mail_requirements': state['cooperation_results']={}
    raw['source']['source_version']='forged'
    state['cooperation_pending_result']=raw
    state['cooperation_progress']['slots']={'status':'running','result_ref':None}
    if phase=='mail_requirements': state['cooperation_progress']['lookup']={'status':'running','result_ref':None}
    state['cooperation_progress']['draft']={'status':'pending','result_ref':None}
    value=await CooperationScheduler(registry,env.source)._verify(state)
    assert value['cooperation_results'][phase]['status']=='unavailable'
    assert value['cooperation_progress'][state['cooperation_current_step']]['status']=='unavailable'
    assert not value['cooperation_results'][phase]['candidate_ids']


@pytest.mark.asyncio
async def test_write_timeout_is_not_automatically_retried():
    env=scheduled()
    await env.graph.ainvoke(env.state,env.config)
    env.confirmation.create_approved_event.side_effect=HarnessError('EXECUTION_TIMEOUT','unknown write outcome')
    done=await env.graph.ainvoke(Command(resume='approved'),env.config)
    assert done['cooperation_summary']['status']=='unavailable'
    assert done['cooperation_step_metrics']['draft']['issue_code']=='EXECUTION_TIMEOUT'
    env.confirmation.create_approved_event.assert_awaited_once()
    await env.graph.ainvoke(Command(resume='approved'),env.config)
    env.confirmation.create_approved_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_after_write_audit_failure_recovers_only_verify_not_external_effect():
    env=scheduled()
    await env.graph.ainvoke(env.state,env.config)
    fail=True
    async def record(run,metadata,result):
        nonlocal fail
        if metadata['step_id']=='draft' and fail:
            fail=False
            raise RuntimeError('audit temporarily unavailable')
        return metadata
    env.source.record_scheduled_step.side_effect=record
    with pytest.raises(RuntimeError,match='audit temporarily'):
        await env.graph.ainvoke(Command(resume='approved'),env.config)
    env.confirmation.create_approved_event.assert_awaited_once()
    done=await env.graph.ainvoke(None,env.config)
    assert done['cooperation_summary']['status']=='completed'
    env.confirmation.create_approved_event.assert_awaited_once()
    assert env.query.query.await_count==1
    assert env.confirmation.create_recommended_draft.await_count==1


@pytest.mark.asyncio
async def test_process_cancellation_is_not_converted_to_success_or_retry():
    import asyncio
    env=scheduled()
    env.query.query.side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await env.graph.ainvoke(env.state,env.config)
    env.source.record_summary.assert_not_awaited()
    env.confirmation.query_available_slots.assert_not_awaited()
    env.confirmation.create_approved_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_failure_preserves_code_and_blocks_later_steps():
    env=scheduled()
    env.query.query.side_effect=HarnessError('EXECUTION_BUDGET_EXCEEDED','budget')
    done=await env.graph.ainvoke(env.state,env.config)
    assert done['cooperation_step_metrics']['lookup']['issue_code']=='EXECUTION_BUDGET_EXCEEDED'
    assert done['cooperation_summary']['blocked_steps']==['slots','draft']
    env.query.query.assert_awaited_once()


@pytest.mark.asyncio
async def test_summary_contains_no_source_body_or_model_output():
    env=scheduled()
    await env.graph.ainvoke(env.state,env.config)
    done=await env.graph.ainvoke(Command(resume='rejected'),env.config)
    text=json.dumps([done['cooperation_summary'],done['cooperation_step_metrics']],ensure_ascii=False)
    assert 'DO_NOT_CHECKPOINT_FULL_BODY' not in text
    assert '请在2026-10-26' not in text
    assert 'LLM' not in text


@pytest.mark.asyncio
async def test_service_audit_usage_unknown_and_boundary_scope():
    from datetime import datetime,timezone
    from uuid import UUID
    from test_harness_layers import Database
    from aegis_agent_worker.repository.run_repository import ClaimedRun
    from aegis_agent_worker.agent.contracts.cooperation import TaskStepResult
    from aegis_agent_worker.service.cooperation.cooperation_service import CooperationService
    repository=SimpleNamespace(execution_status=AsyncMock(return_value='cancelled'),
        draft_valid=AsyncMock(return_value=False),step_usage=AsyncMock(return_value={'model_calls':1,'tool_calls':0,'total_tokens':None}))
    runs=SimpleNamespace(append_event=AsyncMock())
    service=CooperationService(Database(),runs,None,repository=repository)
    run=ClaimedRun(id=UUID(int=1),tenant_id=UUID(int=2),user_id=UUID(int=3),conversation_id=UUID(int=4))
    with pytest.raises(HarnessError) as error: await service.check_execution(run)
    assert error.value.code=='EXECUTION_CANCELLED'
    assert runs.append_event.await_args.kwargs['event_type']=='cooperation_progress'
    assert runs.append_event.await_args.kwargs['payload']['status']=='cancelled'
    runs.append_event.reset_mock()
    result=TaskStepResult(step_id='mail_requirements',status='unavailable',issue_code='tool_unavailable')
    metadata={'step_id':'lookup','workflow':'cooperation_mail_requirements','started_at':datetime.now(timezone.utc).isoformat(),
        'status':'unavailable','plan_digest':'a'*64,'full_prompt':'SECRET','raw_response':'SECRET'}
    safe=await service.record_scheduled_step(run,metadata,result)
    assert safe['usage']=={'model_calls':1,'tool_calls':0,'total_tokens':None,'token_status':'unknown'}
    assert 'SECRET' not in str(runs.append_event.await_args_list)
    assert len(runs.append_event.await_args_list)==2
    assert repository.step_usage.await_args.args[1]==run


@pytest.mark.asyncio
async def test_attempts_are_bound_to_step_and_scope_is_reset():
    from test_harness_layers import Database,Repository,RUN
    from aegis_agent_worker.harness.executor import HarnessExecutor
    from aegis_agent_worker.harness.cooperation_scope import cooperation_step_scope,current_step_tags
    repo=Repository()
    executor=HarnessExecutor(Database(),repository=repo)
    async with executor.scope(RUN):
        with cooperation_step_scope('lookup','a'*64):
            async with executor.attempt('model','fake'): pass
        assert not current_step_tags()
        async with executor.attempt('model','outside'): pass
    assert repo.started[0]['risk_flags']==['cooperation_step:lookup','cooperation_plan:'+'a'*64]
    assert repo.started[1]['risk_flags'] is None


@pytest.mark.asyncio
async def test_repository_filters_attempt_usage_and_draft_by_trusted_identity():
    from uuid import UUID
    from aegis_agent_worker.repository.cooperation_repository import CooperationRepository
    from aegis_agent_worker.repository.run_repository import ClaimedRun
    from aegis_agent_worker.agent.contracts.cooperation import TaskStepResult
    from test_cooperation_workflow import SOURCE
    result=SimpleNamespace(mappings=lambda:SimpleNamespace(one=lambda:{'model_calls':0,'tool_calls':1,'total_tokens':None},
        first=lambda:{'id':UUID(int=8)}))
    session=SimpleNamespace(execute=AsyncMock(return_value=result))
    run=ClaimedRun(id=UUID(int=1),tenant_id=UUID(int=2),user_id=UUID(int=3),conversation_id=UUID(int=4))
    repo=CooperationRepository()
    usage=await repo.step_usage(session,run,'lookup','a'*64)
    query,params=session.execute.await_args.args
    assert 'risk_flags @>' in str(query) and params['tenant']==run.tenant_id and params['user']==run.user_id
    assert json.loads(params['tags'])==['cooperation_step:lookup','cooperation_plan:'+'a'*64]
    assert usage['total_tokens'] is None
    draft=TaskStepResult(step_id='meeting_draft',status='completed',source=SOURCE,draft_id=UUID(int=7),approval_item_id=UUID(int=8))
    assert await repo.draft_valid(session,run,draft)
    query,params=session.execute.await_args.args
    assert 'ai.draft_version=d.version' in str(query) and 'ai.user_id=:user' in str(query)
    assert params['status']=='executed' and params['run']==run.id
