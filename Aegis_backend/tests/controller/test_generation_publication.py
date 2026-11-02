"""Publication HTTP contracts and safe failures; no external services."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import httpx
import pytest
from fastapi import FastAPI,HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from app.controller.knowledge_controller import router
from app.security.dependencies import get_current_user,get_tenant_session
from app.param.generation_publication_param import GenerationPublicationParam
from app.service.generation_publication_service import GenerationPublicationService
from app.param.knowledge_param import KnowledgeIndexStartParam
from test_knowledge_lifecycle import setup,USER,DOC


@pytest.mark.asyncio
async def test_structure_start_freezes_config_without_publication():
    service,repo=setup(title='虚构标题')
    service._settings.knowledge_index_strategy='structure-v1'
    service._settings.knowledge_index_title_enhancement=True
    await service.start_index(None,USER,DOC,KnowledgeIndexStartParam(external_embedding_allowed=True))
    config=repo.create_index_job.await_args.kwargs['index_config']
    assert config['strategy']=='structure-v1' and config['source_title']=='虚构标题'
    assert config['generation']['title_enhancement'] is True
    repo.activation.assert_not_awaited()


@pytest.mark.parametrize('change',[{'tenant_id':str(UUID(int=8))},{'expected_revision':True},
    {'expected_revision':-1},{'rollback':'true'},{'review_reference':''}])
def test_publication_rejects_extra_identity_and_invalid_controls(change):
    with pytest.raises(ValidationError):
        params=dict(version_id=UUID(int=2),generation_id=UUID(int=3),expected_revision=0,review_reference='review')
        GenerationPublicationParam(**(params | change))


@pytest.mark.asyncio
async def test_publication_error_does_not_expose_database_details():
    repo=SimpleNamespace(publish=AsyncMock(side_effect=DBAPIError('private SQL',{},Exception('GENERATION_NOT_READY private data'))))
    param=GenerationPublicationParam(version_id=UUID(int=2),generation_id=UUID(int=3),expected_revision=0,review_reference='review')
    with pytest.raises(HTTPException) as error:await GenerationPublicationService(repo).publish(None,USER,DOC,param)
    assert error.value.status_code==409 and error.value.detail=={'code':'GENERATION_NOT_READY'}


@pytest.mark.asyncio
async def test_publication_http_routes(monkeypatch):
    import app.controller.knowledge_controller as controller
    repo=SimpleNamespace(publish=AsyncMock(return_value=dict(generation_id=UUID(int=3),revision=1,changed=True)),
        status=AsyncMock(return_value=dict(version_id=UUID(int=2),revision=0)))
    monkeypatch.setattr(controller,'GenerationPublicationService',lambda:GenerationPublicationService(repo))
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(user=USER)
    app.dependency_overrides[get_tenant_session]=lambda:None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        url=f'/knowledge/documents/{DOC}/index-publication'
        assert (await c.get(url)).json()['data']['revision']==0
        body=dict(version_id=str(UUID(int=2)),generation_id=str(UUID(int=3)),expected_revision=0,review_reference='review')
        response=await c.post(url,json=body)
        assert response.status_code==200 and response.json()['data']['revision']==1
        assert (await c.post(url,json={**body,'allowed_tools':['anything']})).status_code==422
    repo.publish.assert_awaited_once()
