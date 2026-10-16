"""6.6来源/管理HTTP契约，SQL事实另由真实PG测试验证。"""
from datetime import datetime,UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import httpx
import pytest
import app.controller.knowledge_controller as controller
from fastapi import FastAPI
from app.security.dependencies import get_current_user,get_tenant_session


@pytest.fixture
def endpoint(monkeypatch):
    user=SimpleNamespace(id=UUID(int=10),tenant_id=UUID(int=1))
    doc=dict(document_id=UUID(int=100),version_id=UUID(int=101),job_id=None,title='虚构政策',visibility='private',
        enabled=True,is_sensitive=False,external_embedding_allowed=True,can_manage=True,created_at=datetime.now(UTC),
        index_status='ready',version_label='v1',indexed_chunks=1)
    source=dict(document_id=UUID(int=100),version_id=UUID(int=101),chunk_id=UUID(int=102),title='虚构政策',version_label='v1',
        section='正文',start_offset=0,end_offset=4,excerpt='<script>只展示文本</script>',content_hash='a'*64)
    service=SimpleNamespace(detail=AsyncMock(return_value=doc),delete=AsyncMock(return_value=True),source=AsyncMock(return_value=source))
    visibility=SimpleNamespace(set_visibility=AsyncMock(return_value=True))
    imports=SimpleNamespace(list_documents=AsyncMock(return_value=[doc]))
    monkeypatch.setattr(controller,'KnowledgeDocumentService',lambda:service)
    monkeypatch.setattr(controller,'KnowledgeService',lambda:visibility)
    app=FastAPI()
    app.include_router(controller.router)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(user=user)
    app.dependency_overrides[get_tenant_session]=lambda:None
    app.dependency_overrides[controller.service]=lambda:imports
    return app,service,visibility,imports,user


@pytest.mark.asyncio
async def test_detail_pagination_visibility_and_soft_delete(endpoint):
    app,service,visibility,imports,user=endpoint
    path=f'/knowledge/documents/{UUID(int=100)}'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        result=await client.get(path)
        assert result.status_code==200 and result.json()['data']['version_label']=='v1'
        assert 'storage_key' not in result.text and 'embedding_api_key' not in result.text
        assert (await client.get('/knowledge/documents?offset=100&limit=20')).status_code==200
        assert imports.list_documents.await_args.args[-2:]==(20,100)
        assert (await client.get('/knowledge/documents?offset=-1')).status_code==422
        assert (await client.patch(path+'/visibility',json={'visibility':'tenant'})).status_code==200
        assert visibility.set_visibility.await_args.args[1]==user
        assert (await client.patch(path+'/visibility',json={'visibility':'tenant','user_id':'fake'})).status_code==422
        result=await client.delete(path)
        assert result.status_code==204 and result.content==b''


@pytest.mark.asyncio
async def test_source_hash_bound_no_store_and_404_is_uniform(endpoint):
    app,service,*_=endpoint
    path=f'/knowledge/documents/{UUID(int=100)}/versions/{UUID(int=101)}/chunks/{UUID(int=102)}'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.get(path)).status_code==422
        assert (await client.get(path+'?content_hash=bad')).status_code==422
        result=await client.get(path+'?content_hash='+'a'*64)
        assert result.status_code==200 and result.headers['cache-control']=='no-store'
        assert service.source.await_args.args[-1]=='a'*64
        service.source.return_value=None
        result=await client.get(path+'?content_hash='+'a'*64)
        assert result.status_code==404 and result.json()['detail']['code']=='KNOWLEDGE_SOURCE_UNAVAILABLE'


@pytest.mark.asyncio
async def test_unknown_or_non_owner_management_is_404(endpoint):
    app,service,visibility,*_=endpoint
    service.detail.return_value=None
    service.delete.return_value=False
    visibility.set_visibility.return_value=False
    path=f'/knowledge/documents/{UUID(int=999)}'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.get(path)).status_code==404
        assert (await client.delete(path)).status_code==404
        assert (await client.patch(path+'/visibility',json={'visibility':'private'})).status_code==404
