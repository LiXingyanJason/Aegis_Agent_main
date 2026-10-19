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
