"""取消确认与上下文追溯：无正文、归属前置、未知值不伪装成0。"""
from datetime import datetime,timezone
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from app.param.memory_revoke_param import MemoryRevokeParam
from app.controller import memory_controller,run_controller
from app.security.dependencies import get_current_user,get_tenant_session
from app.service.memory_context_summary_service import MemoryContextSummaryService
from app.repository.memory_lifecycle_repository import MemoryWriteError


@pytest.mark.parametrize('payload',[{}, {'expected_version':True},{'expected_version':0},
    {'expected_version':1,'status':'confirmed'},{'expected_version':1,'content':'cannot edit'}])
def test_revoke_strict_version_only(payload):
    with pytest.raises(ValidationError): MemoryRevokeParam.model_validate(payload)


@pytest.mark.asyncio
async def test_summary_ownership_denied_before_usage_query():
    runs=SimpleNamespace(ensure_run_owned=AsyncMock(return_value=False))
    repository=SimpleNamespace(attempts=AsyncMock())
    assert await MemoryContextSummaryService(runs,repository).get(None,object(),uuid4()) is None
    repository.attempts.assert_not_awaited()


@pytest.mark.asyncio
async def test_summary_returns_whitelisted_metadata_and_current_revocation_state_only():
    attempt,run,memory=uuid4(),uuid4(),uuid4()
    secret='NEVER_RETURN_PROMPT_OR_BODY'
    usage=dict(attempt_id=attempt,memory_id=memory,memory_version=2,memory_type='entity',source_kind='knowledge_document',
        source_object_id=uuid4(),source_version='private-version',included_in_prompt=True,selection_reason='relevant_confirmed_preference',
        current_status='inactive',content=secret,display_summary=secret)
    rows=[dict(id=attempt,purpose='mail_reply_draft',status='succeeded',started_at=datetime.now(timezone.utc),
        memory_count=1,memory_selection_details={'ranking_policy_version':'memory-rank-v1','full_prompt':secret,
            'excluded_counts':{'summary_conflict':2,secret:1,'input_budget':'bad'}},prompt_tokens=None,completion_tokens=None,total_tokens=None)]
    async def usages(*args,legacy=False): return [] if legacy else [usage]
    repository=SimpleNamespace(attempts=AsyncMock(return_value=rows),usages=usages)
    service=MemoryContextSummaryService(SimpleNamespace(ensure_run_owned=AsyncMock(return_value=True)),repository)
    result=await service.get(None,object(),run)
    item=result.attempts[0]
    assert item.needs_review and item.total_tokens is None
    assert item.excluded_counts=={'summary_conflict':2}
    assert item.memories[0].memory_version==2 and item.memories[0].source_object_id is None
    assert item.memories[0].source_version is None and item.memories[0].current_status=='inactive'
    assert secret not in result.model_dump_json() and 'private-version' not in result.model_dump_json()


@pytest.mark.asyncio
async def test_http_revoke_conflict_no_store_and_context_summary_not_found(monkeypatch):
    now=datetime.now(timezone.utc);identifier=uuid4()
    memory=dict(id=identifier,content='说明',source='user_confirmed',source_run_id=None,is_sensitive=False,
        created_at=now,updated_at=now,status='revoked',version=3)
    memory_service=SimpleNamespace(revoke_memory=AsyncMock(return_value=memory))
    monkeypatch.setattr(memory_controller,'_memory_service',memory_service)
    monkeypatch.setattr(run_controller,'_memory_context_summary',SimpleNamespace(get=AsyncMock(return_value=None)))
    app=FastAPI();app.include_router(memory_controller.router);app.include_router(run_controller.router)
    app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(user=SimpleNamespace(id=uuid4(),tenant_id=uuid4()))
    app.dependency_overrides[get_tenant_session]=lambda:None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        revoked=await client.post(f'/memories/{identifier}/revoke',json={'expected_version':2})
        assert revoked.status_code==200 and revoked.json()['data']['status']=='revoked'
        assert revoked.headers['cache-control']=='no-store'
        memory_service.revoke_memory.side_effect=MemoryWriteError('MEMORY_VERSION_CONFLICT','版本冲突')
        stale=await client.post(f'/memories/{identifier}/revoke',json={'expected_version':2})
        assert stale.status_code==409
        assert (await client.get(f'/runs/{uuid4()}/context-summary')).status_code==404
        assert (await client.get(f'/runs/{uuid4()}/context-summary?limit=101')).status_code==422
