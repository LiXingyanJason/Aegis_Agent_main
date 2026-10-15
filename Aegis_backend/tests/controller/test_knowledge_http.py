"""真实HTTP编解码，数据库/存储/队列替身，不访问云服务。"""
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID
from datetime import datetime, timezone
import json
import httpx
from fastapi import FastAPI
import pytest
from app.controller.knowledge_controller import router,service,dispatcher
from app.security.dependencies import get_current_user,get_tenant_session
from app.param.knowledge_param import KnowledgeImportMetadata
from app.common.knowledge_body_limit import KnowledgeBodyLimit


@pytest.mark.asyncio
async def test_http_text_and_multipart_import_and_invalid_metadata():
    class Imports:
        max_source_bytes=1024
        async def import_source(self,session,user,metadata,content,filename):
            assert isinstance(metadata,KnowledgeImportMetadata)
            assert content==b'test'
            return dict(document_id=UUID(int=1),version_id=UUID(int=2),job_id=None,status='uploaded')
        async def start_index(self,session,user,document_id,param):
            assert param.external_embedding_allowed
            return dict(document_id=document_id,version_id=UUID(int=2),job_id=UUID(int=3),status='queued',index_status='queued')
        async def activate(self,session,user,document_id,enabled):
            return dict(document_id=document_id,enabled=enabled,index_status='ready')
        async def list_documents(self,session,user,limit):
            return [dict(document_id=UUID(int=1),version_id=UUID(int=2),job_id=None,
                title='test',category='project',visibility='private',index_status='not_indexed',
                enabled=False,is_sensitive=False,external_embedding_allowed=False,
                can_manage=True,created_at=datetime(2026,1,1,tzinfo=timezone.utc))]
    app=FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(user=SimpleNamespace(id=UUID(int=1),tenant_id=UUID(int=2)))
    app.dependency_overrides[get_tenant_session]=lambda:None
    app.dependency_overrides[service]=lambda:Imports()
    dispatch=SimpleNamespace(enqueue_after_commit=Mock())
    app.dependency_overrides[dispatcher]=lambda:dispatch
    param=dict(title='test',category='project',client_request_id='x',external_embedding_allowed=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/knowledge/documents',json={**param,'content':'test'})
        assert response.status_code==201 and response.json()['data']['status']=='uploaded'
        assert response.json()['data']['job_id'] is None and not response.json()['data']['enabled']
        response=await client.post('/knowledge/documents/upload',data={'metadata':json.dumps(param)},files={'file':('test.txt',b'test','text/plain')})
        assert response.status_code==201
        dispatch.enqueue_after_commit.assert_not_called()
        response=await client.post(f'/knowledge/documents/{UUID(int=1)}/index-jobs',json={'external_embedding_allowed':True})
        assert response.status_code==202 and response.json()['data']['status']=='queued'
        dispatch.enqueue_after_commit.assert_called_once()
        response=await client.patch(f'/knowledge/documents/{UUID(int=1)}/activation',json={'enabled':True})
        assert response.status_code==200 and response.json()['data']['enabled']
        response=await client.patch(f'/knowledge/documents/{UUID(int=1)}/activation',json={'enabled':False})
        assert response.status_code==200 and not response.json()['data']['enabled']
        response=await client.get('/knowledge/documents')
        assert response.status_code==200 and response.json()['data'][0]['can_manage'] is True
        assert dispatch.enqueue_after_commit.call_count==1
        response=await client.post('/knowledge/documents/upload',data={'metadata':json.dumps({**param,'storage_key':'unsafe'})},files={'file':('test.txt',b'test')})
        assert response.status_code==422


@pytest.mark.asyncio
async def test_body_limit_rejects_before_parser_and_handler():
    app=FastAPI()
    app.add_middleware(KnowledgeBodyLimit,maximum=16)
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/knowledge/documents',content=b'a'*17)
        assert response.status_code==413
