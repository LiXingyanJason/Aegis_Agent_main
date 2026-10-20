"""协同展示快照的兼容、历史恢复及授权SQL；不连接业务数据库。"""
import json
from datetime import UTC,datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from test_run_controller import _Result,_ScriptedSession,_create_test_app,LOCAL_USER,RUN_ID,CONVERSATION_ID
from app.vo.cooperation_progress_vo import cooperation_snapshot


def progress(mode='scheduled'):
    return dict(schema_version=1,execution_mode=mode,stage='step_started',status='running',
        label='日历助手正在查询空闲时间',phase='calendar_candidates',domain='calendar',step_id='slots',event_no=12)


def row(snapshot):
    now=datetime(2026,10,19,tzinfo=UTC)
    return dict(id=RUN_ID,conversation_id=CONVERSATION_ID,status='running',current_stage='执行中',
        model_provider='fake',model_name='fake',result_summary=None,error_code=None,error_message=None,
        started_at=now,finished_at=None,created_at=now,cooperation=snapshot)


@pytest.mark.parametrize('mode',['fixed','scheduled'])
def test_run_detail_restores_latest_cooperation_snapshot(mode):
    session=_ScriptedSession([_Result(first_row=row(progress(mode))),_Result(rows=[])])
    with TestClient(_create_test_app(session)) as client:
        response=client.get(f'/runs/{RUN_ID}',headers={'Authorization':'Bearer test'})
    assert response.status_code==200
    result=response.json()['data']
    assert result['cooperation']['execution_mode']==mode and result['cooperation']['event_no']==12
    assert result['status']=='running' and result['steps']==[]
    assert session.calls[0]['tenant_id']==LOCAL_USER.tenant_id and session.calls[0]['user_id']==LOCAL_USER.id


@pytest.mark.parametrize('value',[None,'not json',{'schema_version':99}])
def test_legacy_or_invalid_display_snapshot_does_not_break_run_query(value):
    session=_ScriptedSession([_Result(first_row=row(value)),_Result(rows=[])])
    with TestClient(_create_test_app(session)) as client:
        result=client.get(f'/runs/{RUN_ID}',headers={'Authorization':'Bearer test'})
    assert result.status_code==200 and result.json()['data']['cooperation'] is None


def test_snapshot_drops_raw_or_execution_fields():
    value=cooperation_snapshot(json.dumps({**progress(),'body':'PRIVATE','approval_item_id':'FORGED','prompt':'PRIVATE'}))
    assert value.event_no==12 and 'PRIVATE' not in value.model_dump_json() and 'FORGED' not in value.model_dump_json()


@pytest.mark.asyncio
async def test_latest_progress_sql_is_correlated_to_owned_run_and_tenant():
    from app.repository.run_repository import AgentRunRepository
    from app.repository.conversation_repository import ConversationRepository
    result=SimpleNamespace(mappings=lambda:SimpleNamespace(first=lambda:None,all=lambda:[]))
    session=SimpleNamespace(execute=AsyncMock(return_value=result))
    await AgentRunRepository().find_run_detail_for_user(session,run_id=RUN_ID,tenant_id=LOCAL_USER.tenant_id,user_id=LOCAL_USER.id)
    sql,params=session.execute.await_args.args
    assert "e.event_type='cooperation_progress'" in str(sql)
    assert 'e.run_id=agent_runs.id' in str(sql) and 'e.tenant_id=agent_runs.tenant_id' in str(sql)
    assert 'user_id = :user_id' in str(sql) and params['user_id']==LOCAL_USER.id
    await ConversationRepository().list_runs_for_conversation(session,conversation_id=CONVERSATION_ID,
        tenant_id=LOCAL_USER.tenant_id,user_id=LOCAL_USER.id)
    sql,params=session.execute.await_args.args
    assert 'ORDER BY e.event_no DESC LIMIT 1' in str(sql)
    assert params['tenant_id']==LOCAL_USER.tenant_id and params['user_id']==LOCAL_USER.id


@pytest.mark.asyncio
async def test_history_controller_restores_snapshot_without_tool_or_approval_actions(monkeypatch):
    from app.controller import conversation_controller as controller
    from app.security.dependencies import CurrentUser
    now=datetime.now(UTC)
    run={**row(progress()),'steps':[],'tool_previews':[],'approval_items':[]}
    service=SimpleNamespace(get_conversation_detail=AsyncMock(return_value={
        'conversation':dict(title='历史',status='active',last_message_at=now,created_at=now,updated_at=now),
        'messages':[],'runs':[run]}))
    monkeypatch.setattr(controller,'_conversation_service',service)
    result=await controller.get_conversation_detail(CONVERSATION_ID,CurrentUser(user=LOCAL_USER),object())
    assert result['data'].runs[0].cooperation.execution_mode=='scheduled'
    assert result['data'].runs[0].approval_items==[]
