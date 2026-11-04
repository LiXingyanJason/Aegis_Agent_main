from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
import httpx
from fastapi import FastAPI
from app.param.memory_formation_param import MemoryFormationParam
from app.controller.memory_controller import router
from app.security.dependencies import get_current_user,get_tenant_session


@pytest.mark.parametrize('changes',[{'mode':'controlled_auto'},{'expected_version':True},{'tenant_id':str(uuid4())},
    {'mode':'suggest','auto_approval_consent':True},{'allowed_categories':['secret']},{'external_processing_allowed':'true'}])
def test_settings_require_explicit_and_typed_controls(changes):
    with pytest.raises(ValueError):MemoryFormationParam(**({'expected_version':0}|changes))


@pytest.mark.asyncio
async def test_http_static_settings_route_and_cas(monkeypatch):
    from app.service.memory_formation_service import MemoryFormationService
    user=SimpleNamespace(id=uuid4(),tenant_id=uuid4())
    repo=SimpleNamespace(read=AsyncMock(return_value={'version':0,'mode':'suggest'}),update=AsyncMock(return_value={'version':1,'mode':'off'}))
    monkeypatch.setattr(MemoryFormationService,'__init__',lambda self:setattr(self,'repository',repo))
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(user=user)
    app.dependency_overrides[get_tenant_session]=lambda:None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        response=await c.get('/memories/formation-settings')
        assert response.status_code==200 and response.json()['data']['version']==0
        response=await c.put('/memories/formation-settings',json={'expected_version':0,'mode':'off'})
        assert response.status_code==200 and response.json()['data']['mode']=='off'
        assert response.headers['cache-control']=='no-store'
        assert (await c.put('/memories/formation-settings',json={'expected_version':0,'user_id':str(uuid4())})).status_code==422
