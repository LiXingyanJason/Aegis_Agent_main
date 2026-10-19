"""清理工作缓存的认证边界和Redis失败HTTP契约。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from fastapi import FastAPI
import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.controller.session_memory_controller import router
from app.security.dependencies import get_current_user


def test_api_worker_redis_invalidation_protocol_matches_without_runtime_import():
    import ast
    from pathlib import Path
    from app.service.session_memory_service import INVALIDATE_SCRIPT
    worker=Path(__file__).resolve().parents[3]/'aegis_agent_worker/src/aegis_agent_worker/storage/session_memory_cache.py'
    tree=ast.parse(worker.read_text(encoding='utf-8'))
    statement=next(node for node in tree.body if isinstance(node,ast.Assign)
                   and any(isinstance(target,ast.Name) and target.id=='INVALIDATE_SCRIPT' for target in node.targets))
    assert ast.literal_eval(statement.value)==INVALIDATE_SCRIPT


@pytest.mark.asyncio
async def test_clear_current_user_only_no_prompt_or_db_side_effects():
    app=FastAPI()
    app.include_router(router)
    app.state.redis=SimpleNamespace(eval=AsyncMock(return_value=2))
    user=SimpleNamespace(tenant_id=UUID(int=1),id=UUID(int=2))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.delete('/session-memory')).status_code==401
        app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(user=user)
        response=await client.delete('/session-memory?user_id=forged')
        assert response.status_code==204 and response.content==b''
        assert response.headers['cache-control']=='no-store'
    keys=app.state.redis.eval.await_args.args[2:]
    assert all(str(user.tenant_id) in key and str(user.id) in key and 'forged' not in key for key in keys)


@pytest.mark.asyncio
async def test_failed_cache_clear_is_explicit_not_silent_success():
    app=FastAPI()
    app.include_router(router)
    app.state.redis=SimpleNamespace(eval=AsyncMock(side_effect=RedisConnectionError('secret connection text')))
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(user=SimpleNamespace(tenant_id=UUID(int=1),id=UUID(int=2)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.delete('/session-memory')
    assert response.status_code==503 and 'SESSION_CACHE_UNAVAILABLE' in response.text
    assert 'secret' not in response.text
