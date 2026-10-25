from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from app.controller.skill_controller import router
from app.param.skill_param import SkillContent,SkillRevision,SkillEnabled,SkillUpdate
from app.service.skill_service import SkillService,content_hash

def test_template_is_valid_and_purposes_are_bounded():
    template=SkillService().template()
    assert SkillContent(**template).name=='renewal-reply'
    for changes in ({'purposes':['main_routing']},{'path':'../../test'},{'allowed_tools':['mail.messages.send']},{'body':'x'*6001}):
        with pytest.raises(ValueError): SkillContent(**{**template,**changes})
    assert len(content_hash(SkillContent(**template)))==64

@pytest.mark.asyncio
async def test_unowned_and_stale_mutation_never_writes():
    repo=SimpleNamespace(get=AsyncMock(return_value=None),publish=AsyncMock(),event=AsyncMock())
    service=SkillService(repo)
    for row,code in ((None,404),({'revision':3},409)):
        repo.get.return_value=row
        with pytest.raises(HTTPException) as error:
            await service.change(None,None,uuid4(),SkillRevision(expected_revision=1),'published')
        assert error.value.status_code==code
    repo.publish.assert_not_awaited();repo.event.assert_not_awaited()

@pytest.mark.asyncio
async def test_unpublished_cannot_enable_and_edit_does_not_publish():
    repo=SimpleNamespace(get=AsyncMock(return_value={'revision':1,'published_version':None}),
        state=AsyncMock(),update=AsyncMock(return_value={'revision':2}),event=AsyncMock(),publish=AsyncMock())
    service=SkillService(repo)
    with pytest.raises(HTTPException):
        await service.change(None,None,uuid4(),SkillEnabled(expected_revision=1,enabled=True),'enabled')
    repo.state.assert_not_awaited()
    await service.change(None,None,uuid4(),SkillUpdate(**service.template(),expected_revision=1),'edited')
    repo.update.assert_awaited_once();repo.publish.assert_not_awaited()

def test_routes_require_authentication_and_openapi_exposes_contract():
    app=FastAPI();app.include_router(router)
    with TestClient(app) as client:
        assert client.get('/skills').status_code==401
        assert client.get('/skills/templates').status_code==401
        assert client.post('/skills',json=SkillService().template()).status_code==401
        spec=client.get('/openapi.json').json()
        assert '/skills/{skill_id}/publish' in spec['paths']
        assert spec['components']['schemas']['SkillContent']['additionalProperties'] is False
