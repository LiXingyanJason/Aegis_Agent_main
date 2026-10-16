"""知识阶段/Artifact沿用SSE协议及Last-Event-ID恢复，不在事件中夹带来源正文。"""
from contextlib import asynccontextmanager
from datetime import datetime,UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import httpx
import pytest
from fastapi import FastAPI
import app.controller.run_controller as controller
from app.security.dependencies import get_current_user
from app.config.database import TenantContext
from app.event.run_event import RunEvent


@pytest.mark.asyncio
async def test_knowledge_sse_replay_stages_and_artifact_refresh_only(monkeypatch):
    tenant,user,run=UUID(int=1),UUID(int=10),UUID(int=600)
    history=[RunEvent(run_id=run,event_no=index,event_type=kind,payload=payload,created_at=datetime.now(UTC))
        for index,(kind,payload) in enumerate([
            ('progress_updated',{'step_id':str(UUID(int=700)),'label':'正在规划知识查询','status':'running'}),
            ('progress_updated',{'step_id':str(UUID(int=701)),'label':'正在检索知识资料','status':'succeeded'}),
            ('progress_updated',{'step_id':str(UUID(int=702)),'label':'正在生成来源约束回答','status':'succeeded'}),
            ('artifacts_ready',{'conversation_id':str(UUID(int=900)),'artifact_ids':[str(UUID(int=800))],'artifact_types':['knowledge_sources']}),
            ('run_completed',{'status':'completed'}),
        ],1)]
    class Database:
        @asynccontextmanager
        async def session(self):
            class Session:
                @asynccontextmanager
                async def begin(self): yield
                async def execute(self,*args): pass
            yield Session()
    async def events(session,current,identifier,after): return [item for item in history if item.event_no>after]
    monkeypatch.setattr(controller,'_run_service',SimpleNamespace(ensure_run_owned=AsyncMock(return_value=True),list_run_events=events))
    pubsub=SimpleNamespace(subscribe=AsyncMock(),unsubscribe=AsyncMock(),aclose=AsyncMock())
    app=FastAPI()
    app.state.database=Database()
    app.state.redis=SimpleNamespace(pubsub=lambda:pubsub)
    app.include_router(controller.router)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(user=SimpleNamespace(id=user,tenant_id=tenant),tenant_context=TenantContext(tenant,user))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        result=await client.get(f'/runs/{run}/events',headers={'Last-Event-ID':'3'})
        assert result.status_code==200 and result.headers['content-type'].startswith('text/event-stream')
        assert 'id: 4\nevent: artifacts_ready' in result.text and 'knowledge_sources' in result.text
        assert 'id: 5\nevent: run_completed' in result.text and 'id: 3' not in result.text
        assert 'excerpt' not in result.text and 'content_hash' not in result.text
