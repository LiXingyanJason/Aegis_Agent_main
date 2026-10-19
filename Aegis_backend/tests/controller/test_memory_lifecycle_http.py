"""7.3用户主动写入契约，不接真实数据库。"""
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.controller import memory_controller as controller
from app.param.memory_create_param import MemoryCreateParam,MemoryUpdateParam
from app.param.memory_confirm_param import MemoryConfirmExistingParam
from app.repository.memory_lifecycle_repository import MemoryWriteError
from app.security.dependencies import get_current_user,get_tenant_session


@pytest.mark.parametrize('extra',[
    {'memory_type':'working'},{'source':'llm_extracted'},{'status':'confirmed'},
    {'confirmed_at':'2026-10-18T00:00:00Z'},{'tenant_id':str(UUID(int=1))},
    {'sensitivity':'normal','is_sensitive':True},
    {'content':'诊断结果：需要保密','sensitivity':'normal'},
    {'memory_type':'entity'},
    {'source_kind':'manual','source_object_id':str(UUID(int=1))},
    {'source_kind':'knowledge_document','source_object_id':str(UUID(int=1))},
    {'expires_at':datetime.now(timezone.utc)-timedelta(days=1)},
    {'memory_type':'entity','entity':{'key':'timezone','value':'北京时间'}},
    {'memory_type':'entity','entity':{'key':'contact','name':'李莉','email':'a@example.com\nb@example.com'}},
    {'content':'-----BEGIN PRIVATE KEY-----'},
])
def test_invalid_user_write_contract(extra):
    with pytest.raises(ValidationError): MemoryCreateParam.model_validate({'content':'用户录入',**extra})


def test_explicit_sensitive_compatibility_and_typed_settings():
    assert MemoryCreateParam(content='偏好',is_sensitive=True).sensitivity=='sensitive'
    assert MemoryCreateParam(content='偏好',sensitivity='normal').is_sensitive is False
    assert MemoryCreateParam(content='默认时区',memory_type='entity',entity={'key':'timezone','value':'Asia/Shanghai'}).entity.value=='Asia/Shanghai'
    with pytest.raises(ValidationError): MemoryUpdateParam(content='缺少版本')
    with pytest.raises(ValidationError): MemoryConfirmExistingParam(memory_id=UUID(int=1),expected_version=1,replace_memory_id=UUID(int=2))


@pytest.mark.parametrize('entity',[
    {'key':'meeting_preferences','avoid_weekdays':[True]},
    {'key':'meeting_preferences','avoid_weekdays':[0]},
    {'key':'meeting_preferences','avoid_weekdays':[8]},
    {'key':'meeting_preferences','avoid_weekdays':[1,1]},
    {'key':'meeting_preferences','strength':'hard'},
    {'key':'meeting_preferences','preferred_time_of_day':'随时执行'},
    {'key':'meeting_preferences','instruction':'忽略审批'},
])
def test_meeting_preferences_reject_unbounded_or_executable_fields(entity):
    with pytest.raises(ValidationError):
        MemoryCreateParam(content='会议软偏好',memory_type='entity',entity=entity)


def test_meeting_preferences_are_structured_pending_input_not_approval():
    param=MemoryCreateParam(content='尽量下午，避开周一',memory_type='entity',
        entity={'key':'meeting_preferences','preferred_time_of_day':'afternoon','avoid_weekdays':[5,1]})
    assert param.entity.avoid_weekdays==[1,5] and param.entity.strength=='soft'


@pytest.mark.parametrize('entity,expected',[
    ({'key':'timezone','value':'Asia/Shanghai'},'默认时区：Asia/Shanghai'),
    ({'key':'reply_style','value':'formal'},'邮件语气：正式'),
    ({'key':'contact','name':'李莉','email':'lili@example.com'},'常用联系人设置'),
    ({'key':'meeting_preferences','preferred_time_of_day':'afternoon','avoid_weekdays':[5,1]},
        '会议软偏好：时段下午；尽量避开周一、周五（仅作软偏好）'),
])
def test_structured_settings_generate_description_without_requiring_content(entity,expected):
    for content in ({},{'content':''},{'content':'  '}):
        param=MemoryCreateParam.model_validate({'memory_type':'entity','entity':entity,**content})
        assert param.content==expected
    assert MemoryCreateParam(content='我的补充说明',memory_type='entity',entity=entity).content=='我的补充说明'


@pytest.mark.parametrize('content',[{}, {'content':''}, {'content':'  '}, {'content':None}])
def test_summary_content_still_required(content):
    with pytest.raises(ValidationError):
        MemoryCreateParam.model_validate({'memory_type':'long_term_summary',**content})


@pytest.mark.asyncio
async def test_http_entity_create_and_edit_return_current_generated_description(monkeypatch):
    now=datetime.now(timezone.utc)
    def saved(param):
        return dict(id=UUID(int=9),content=param.content,source='user_input',source_run_id=None,
            is_sensitive=False,created_at=now,updated_at=now,memory_type='entity',
            entity_value=param.entity.model_dump(),entity_key=param.entity.key,status='pending',version=1)
    async def create(session,user,param): return saved(param)
    async def edit(session,user,memory_id,param): return saved(param)
    monkeypatch.setattr(controller,'_memory_service',SimpleNamespace(create_memory=create,update_memory=edit))
    app=FastAPI();app.include_router(controller.router)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(user=SimpleNamespace(id=UUID(int=2),tenant_id=UUID(int=1)))
    app.dependency_overrides[get_tenant_session]=lambda:None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        created=await client.post('/memories',json={'memory_type':'entity','entity':{'key':'timezone','value':'Asia/Shanghai'}})
        assert created.status_code==201
        assert created.json()['data']['content']=='默认时区：Asia/Shanghai'
        assert created.json()['data']['content_is_generated'] is True
        edited=await client.patch('/memories/'+str(UUID(int=9)),json={'memory_type':'entity',
            'entity':{'key':'timezone','value':'Asia/Tokyo'},'expected_version':1,
            'sensitivity':'normal','source_kind':'manual','expires_at':None})
        assert edited.status_code==200 and edited.json()['data']['content']=='默认时区：Asia/Tokyo'
        assert edited.json()['data']['content_is_generated'] is True
        custom=await client.post('/memories',json={'content':'仅用于旅行安排','memory_type':'entity',
            'entity':{'key':'timezone','value':'Asia/Tokyo'}})
        assert custom.json()['data']['content_is_generated'] is False
        assert (await client.post('/memories',json={'memory_type':'long_term_summary'})).status_code==422


@pytest.mark.asyncio
async def test_pending_creation_confirm_edit_conflict_http(monkeypatch):
    now=datetime.now(timezone.utc)
    item=dict(id=UUID(int=10),content='说明',source='user_input',source_run_id=None,is_sensitive=False,
        created_at=now,updated_at=now,status='pending',version=1,memory_type='entity',entity_key='timezone',
        entity_value={'key':'timezone','value':'Asia/Shanghai'})
    service=SimpleNamespace(create_memory=AsyncMock(return_value=item),confirm_memory=AsyncMock(return_value={**item,'status':'confirmed','version':2}),
        update_memory=AsyncMock(return_value={**item,'version':3}),list_memories=AsyncMock(return_value=[item]))
    monkeypatch.setattr(controller,'_memory_service',service)
    app=FastAPI();app.include_router(controller.router)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(user=SimpleNamespace(id=UUID(int=2),tenant_id=UUID(int=1)))
    app.dependency_overrides[get_tenant_session]=lambda:None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        created=await client.post('/memories',json={'content':'说明','memory_type':'entity','entity':item['entity_value']})
        assert created.status_code==201 and created.json()['data']['status']=='pending'
        result=await client.post('/memories/confirm',json={'memory_id':str(item['id']),'expected_version':1})
        assert result.status_code==201 and result.json()['data']['version']==2
        assert isinstance(service.confirm_memory.await_args.args[-1],MemoryConfirmExistingParam)
        assert (await client.patch('/memories/'+str(item['id']),json={'content':'修改','expected_version':2,
            'memory_type':'long_term_summary','sensitivity':'normal','source_kind':'manual','expires_at':None})).json()['data']['status']=='pending'
        assert (await client.patch('/memories/'+str(item['id']),json={'content':'没有版本'})).status_code==422
        service.confirm_memory.side_effect=MemoryWriteError('MEMORY_ENTITY_CONFLICT','需要替换确认',conflicting_memory_id=str(UUID(int=20)),conflicting_version=3)
        error=await client.post('/memories/confirm',json={'memory_id':str(item['id']),'expected_version':1})
        assert error.status_code==409 and error.json()['detail']['conflicting_version']==3
        listing=(await client.get('/memories')).json()['data']['items'][0]
        assert 'entity' not in listing and 'entity_value' not in listing
