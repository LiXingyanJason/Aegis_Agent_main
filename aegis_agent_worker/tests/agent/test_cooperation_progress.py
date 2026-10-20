"""协同进度仅供展示，固定/调度、恢复和安全载荷。"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import pytest
from langgraph.types import Command
from pydantic import ValidationError
from test_cooperation_workflow import setup
from test_cooperation_plan import plan
from aegis_agent_worker.agent.contracts.cooperation_progress import CooperationProgress


@pytest.mark.asyncio
async def test_progress_starts_before_work_and_waits_then_finishes_without_reexecution():
    env=setup(plan_response=json.dumps(plan()))
    async def query(*args,**kwargs):
        events=[call.kwargs for call in env.source.record_progress.await_args_list]
        assert events[0]['stage']=='started'
        assert any(item['stage']=='step_started' and item['phase']=='mail_requirements' for item in events)
        from test_cooperation_workflow import SOURCE
        return ([{'id':SOURCE.id,'source_version':'v1'}],'mock')
    env.query.query.side_effect=query
    await env.graph.ainvoke(env.state,env.config)
    events=[call.kwargs for call in env.source.record_progress.await_args_list]
    assert events[-1]['stage']=='waiting_confirmation'
    assert {item['execution_mode'] for item in events}=={'scheduled'}
    assert sum(item['stage']=='started' for item in events)==1
    assert sum(item['stage']=='waiting_confirmation' for item in events)==1
    assert all(CooperationProgress.model_validate(item).payload()['label'] for item in events)
    env.confirmation.create_approved_event.assert_not_awaited()
    await env.graph.ainvoke(Command(resume='rejected'),env.config)
    events=[call.kwargs for call in env.source.record_progress.await_args_list]
    assert events[-1]['stage']=='finished' and events[-1]['status']=='rejected'
    assert sum(item['stage']=='waiting_confirmation' for item in events)==1
    assert env.query.query.await_count==1
    env.confirmation.create_approved_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_planning_failure_and_blocked_dependency_are_not_success():
    env=setup(plan_response='{}')
    env.main_client.complete.side_effect=[json.dumps({'decision':'delegate','domain':'mail',
        'intent':'arrange_meeting_from_mail','instruction':'按邮件安排会议'}),'{}','{}']
    await env.graph.ainvoke(env.state,env.config)
    events=[call.kwargs for call in env.source.record_progress.await_args_list]
    assert [item['stage'] for item in events]==['started','planning','finished']
    assert events[-1]['status']=='unavailable'
    env.query.query.assert_not_awaited()
    env=setup(plan_response=json.dumps(plan()))
    env.query.query.return_value=([],'mock')
    await env.graph.ainvoke(env.state,env.config)
    events=[call.kwargs for call in env.source.record_progress.await_args_list]
    assert [item['step_id'] for item in events if item['stage']=='blocked']==['slots','draft']
    assert events[-1]['stage']=='finished' and events[-1]['status']=='unavailable'
    assert not any(item['stage']=='step_started' and item['phase']=='calendar_candidates' for item in events)


def test_progress_contract_rejects_body_labels_and_invalid_status_combinations():
    for extra in ({'label':'USER_SECRET'},{'body':'USER_SECRET'},{'tenant_id':str(UUID(int=3))}):
        with pytest.raises(ValidationError): CooperationProgress(execution_mode='scheduled',stage='started',status='running',**extra)
    with pytest.raises(ValidationError): CooperationProgress(execution_mode='scheduled',stage='waiting_confirmation',status='completed')
    with pytest.raises(ValidationError): CooperationProgress(execution_mode='scheduled',stage='step_started',status='running',phase='arbitrary')


@pytest.mark.asyncio
async def test_progress_persisted_and_published_after_commit_only():
    from test_harness_layers import Database
    from aegis_agent_worker.service.cooperation.cooperation_service import CooperationService
    from aegis_agent_worker.repository.run_repository import ClaimedRun
    runs=SimpleNamespace(append_event=AsyncMock(return_value='saved_event'))
    events=SimpleNamespace(publish_after_commit=lambda event:published.append(event))
    published=[]
    service=CooperationService(Database(),runs,None,events=events)
    run=ClaimedRun(id=UUID(int=1),tenant_id=UUID(int=2),user_id=UUID(int=3),conversation_id=UUID(int=4))
    await service.record_progress(run,execution_mode='scheduled',stage='step_started',status='running',
        phase='calendar_candidates',step_id='slots')
    call=runs.append_event.await_args.kwargs
    assert call['run_id']==run.id and call['tenant_id']==run.tenant_id
    assert call['event_type']=='cooperation_progress' and call['payload']['label']=='日历助手正在查询空闲时间'
    assert published==['saved_event']
    runs.append_event.side_effect=RuntimeError('rollback')
    with pytest.raises(RuntimeError):
        await service.record_progress(run,execution_mode='scheduled',stage='started',status='running')
    assert published==['saved_event']
