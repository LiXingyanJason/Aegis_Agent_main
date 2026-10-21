"""8.6：数据库为权威的SSE补发，通知丢失/伪造不能漏事件或生成工具动作。"""
from contextlib import asynccontextmanager
from datetime import datetime,UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import app.controller.run_controller as controller
from app.event.run_event import RunEvent
from app.config.database import TenantContext
from app.repository.run_repository import AgentRunRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("notification",[None,{"data":"malformed or forged terminal"}])
async def test_missing_notification_or_fake_payload_reads_ordered_authoritative_history(monkeypatch,notification):
    run=UUID(int=10);tenant=UUID(int=1);user=UUID(int=2)
    rows=[];cursors=[]
    kinds=["cooperation_progress","tool_preview","input_required","cooperation_progress","run_completed"]
    history=[RunEvent(run,index,kind,{"phase":"finished"} if index==4 else {},datetime.now(UTC))
        for index,kind in enumerate(kinds,1)]
    class DB:
        @asynccontextmanager
        async def session(self):
            class Session:
                @asynccontextmanager
                async def begin(self): yield
                async def execute(self,*args): pass
            yield Session()
    async def read(session,current,identifier,after):
        cursors.append(after)
        return [row for row in rows if row.event_no>after]
    async def wake(**kwargs):
        rows.extend(history)
        return notification
    monkeypatch.setattr(controller,"_run_service",SimpleNamespace(list_run_events=read))
    pubsub=SimpleNamespace(subscribe=AsyncMock(),get_message=wake,unsubscribe=AsyncMock(),aclose=AsyncMock())
    request=SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    current=SimpleNamespace(user=SimpleNamespace(id=user,tenant_id=tenant),tenant_context=TenantContext(tenant,user))
    frames=[frame async for frame in controller._run_event_stream(request=request,database=DB(),
        redis=SimpleNamespace(pubsub=lambda:pubsub),run_id=run,current_user=current,after_event_no=0)]
    assert len(frames)==5 and cursors==[0,0]
    assert [int(frame.splitlines()[0][4:]) for frame in frames]==[1,2,3,4,5]
    assert "forged" not in "".join(frames)
    assert "event: run_completed" in frames[-1]  # 团队finished不是SSE终态。
    pubsub.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_replay_rechecks_user_ownership_in_event_sql():
    captured=[]
    class Result:
        def mappings(self): return self
        def all(self): return []
    class Session:
        async def execute(self,sql,args): captured.append((str(sql),args)); return Result()
    await AgentRunRepository().list_events_for_run(Session(),run_id=UUID(int=10),tenant_id=UUID(int=1),
        user_id=UUID(int=2),after_event_no=7)
    assert "ar.user_id=:user_id" in captured[0][0] and captured[0][1]["after_event_no"]==7


@pytest.mark.asyncio
async def test_redis_unavailable_still_replays_after_cursor_and_closes_on_real_run_terminal(monkeypatch):
    run,tenant,user=UUID(int=10),UUID(int=1),UUID(int=2)
    rows=[RunEvent(run,index,kind,{},datetime.now(UTC)) for index,kind in enumerate(
        ["cooperation_progress","input_required","run_completed"],1)]
    class DB:
        @asynccontextmanager
        async def session(self):
            class Session:
                @asynccontextmanager
                async def begin(self): yield
                async def execute(self,*args): pass
            yield Session()
    async def read(session,current,identifier,after): return [row for row in rows if row.event_no>after]
    monkeypatch.setattr(controller,"_run_service",SimpleNamespace(list_run_events=read))
    pubsub=SimpleNamespace(subscribe=AsyncMock(side_effect=RuntimeError("Redis unavailable")),aclose=AsyncMock())
    current=SimpleNamespace(user=SimpleNamespace(id=user,tenant_id=tenant),tenant_context=TenantContext(tenant,user))
    frames=[frame async for frame in controller._run_event_stream(request=SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
        database=DB(),redis=SimpleNamespace(pubsub=lambda:pubsub),run_id=run,current_user=current,after_event_no=1)]
    assert len(frames)==2 and frames[0].startswith("id: 2") and "event: run_completed" in frames[1]
    pubsub.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_input_refresh_joins_display_association_from_matching_persisted_question_event():
    from app.repository.agent_input_repository import AgentInputRepository
    sqls=[]
    class Result:
        def mappings(self): return self
        def all(self): return []
    class Session:
        async def execute(self,sql,args): sqls.append(str(sql)); return Result()
    await AgentInputRepository().list_inputs(Session(),SimpleNamespace(id=UUID(int=2),tenant_id=UUID(int=1)),UUID(int=10))
    query=sqls[-1]
    assert "q.tenant_id=:tenant AND q.user_id=:user" in query
    assert "e.payload->>'question_id'=q.id::text" in query and "association.cooperation" in query
