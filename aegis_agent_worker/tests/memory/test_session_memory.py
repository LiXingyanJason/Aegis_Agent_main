"""7.2会话缓存服务与协议测试；Redis/SQL均为替身。"""
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from aegis_agent_worker.config.database import TenantContext
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.repository.session_memory_repository import SessionMemoryRepository
from aegis_agent_worker.service.memory.session_memory_service import SessionMemoryService
from aegis_agent_worker.service.runtime.conversation_context_service import ConversationContextService
from aegis_agent_worker.storage.session_memory_cache import RedisSessionMemoryCache,cache_keys,PUT_SCRIPT
from aegis_agent_worker.harness.policy_provider import DEFAULT_POLICIES,LEGACY_POLICY,PolicyProvider


RUN=ClaimedRun(UUID(int=1),UUID(int=2),UUID(int=3),UUID(int=4))
CONTEXT=TenantContext(RUN.tenant_id,RUN.user_id)
POLICY=DEFAULT_POLICIES.current().session_memory


@asynccontextmanager
async def scope(*args,**kwargs):
    yield


def scenario(monkeypatch):
    import aegis_agent_worker.service.memory.session_memory_service as module
    import aegis_agent_worker.service.runtime.conversation_context_service as context_module
    monkeypatch.setattr(module,'tenant_transaction',scope)
    monkeypatch.setattr(context_module,'tenant_transaction',scope)
    database=SimpleNamespace(session=scope)
    row=dict(sequence_no=12,snapshot_tick=100,tasks=[dict(id=UUID(int=10),run_type='conversation',
        status='completed',content='此字段不得进入缓存')],cards=[dict(id=UUID(int=11),run_id=UUID(int=10),
        artifact_type='mail_candidates',source_version='a'*32,expires_at='2026-10-18T00:00:00+00:00',
        payload={'body':'秘密正文不能复制'})])
    repo=SimpleNamespace(snapshot=AsyncMock(return_value=row))
    cache=SimpleNamespace(read=AsyncMock(return_value=('0',None)),put=AsyncMock(return_value=1),
                          invalidate_user=AsyncMock())
    service=SessionMemoryService(database,cache,repo)
    return service,repo,cache,row,database


@pytest.mark.asyncio
async def test_miss_rebuild_then_hit_revalidates_sql_no_raw_content(monkeypatch):
    service,repo,cache,row,_=scenario(monkeypatch)
    facts=await service.load(RUN,CONTEXT)
    assert '秘密' not in json.dumps(facts,ensure_ascii=False) and 'content' not in json.dumps(facts)
    payload=cache.put.await_args.args[-1]
    cache.read.return_value=('0',payload)
    cache.put.reset_mock()
    assert await service.load(RUN,CONTEXT)==facts
    assert repo.snapshot.await_count==2
    cache.put.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['sequence','status','card_version','expired_card','forged_cache','wrong_user','old_generation','wrong_policy'])
async def test_stale_or_forged_cache_never_delivered(monkeypatch,change):
    service,repo,cache,row,_=scenario(monkeypatch)
    await service.load(RUN,CONTEXT)
    cached=deepcopy(cache.put.await_args.args[-1])
    if change=='sequence': row['sequence_no']+=1
    if change=='status': row['tasks'][0]['status']='failed'
    if change=='card_version': row['cards'][0]['source_version']='b'*32
    if change=='expired_card': row['cards']=[]
    if change=='forged_cache': cached['facts']['cards'][0]['query_id']='forged'
    if change=='wrong_user': cached['user_id']=str(UUID(int=999))
    if change=='old_generation': cached['generation']='-1'
    if change=='wrong_policy': cached['policy_version']='unknown'
    cache.read.return_value=('0',cached)
    cache.put.reset_mock()
    facts=await service.load(RUN,CONTEXT)
    cache.put.assert_awaited_once()
    assert facts==service._facts(row,POLICY)
    assert 'forged' not in json.dumps(facts)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['read','put'])
@pytest.mark.parametrize('error',[RedisConnectionError('no redis'),TimeoutError()])
async def test_redis_failure_uses_current_database_facts(monkeypatch,operation,error,caplog):
    service,repo,cache,row,_=scenario(monkeypatch)
    getattr(cache,operation).side_effect=error
    assert await service.load(RUN,CONTEXT)==service._facts(row,POLICY)
    assert 'session_memory_degraded' in caplog.text
    assert '秘密' not in caplog.text


@pytest.mark.asyncio
async def test_identity_and_missing_conversation_fail_closed(monkeypatch):
    service,repo,cache,row,_=scenario(monkeypatch)
    with pytest.raises(ValueError,match='IDENTITY'):
        await service.load(RUN,TenantContext(UUID(int=999),RUN.user_id))
    repo.snapshot.assert_not_awaited()
    repo.snapshot.return_value=None
    with pytest.raises(ValueError,match='UNAVAILABLE'):
        await service.load(RUN,CONTEXT)
    cache.invalidate_user.assert_awaited_once_with(RUN.tenant_id,RUN.user_id)
    cache.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_page_tasks_skip_session_cache(monkeypatch):
    service,repo,cache,_,_=scenario(monkeypatch)
    assert await service.load(replace(RUN,run_type='mail_extraction',conversation_id=None),CONTEXT) is None
    repo.snapshot.assert_not_awaited()
    cache.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_policy_bounds_entire_snapshot_and_oversize_uses_history_only(monkeypatch):
    service,repo,cache,row,_=scenario(monkeypatch)
    strict=replace(POLICY,max_cache_bytes=1)
    service._policies=PolicyProvider((replace(LEGACY_POLICY,session_memory=strict),))
    assert await service.load(RUN,CONTEXT) is None
    assert cache.read.await_args.kwargs['policy'] is strict
    cache.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_bounded_history_and_followup_keep_real_latest_user_message(monkeypatch):
    service,repo,cache,row,database=scenario(monkeypatch)
    runs=SimpleNamespace(load_conversation_history=AsyncMock(return_value=[
        {'role':'user' if i%2==0 else 'assistant','content':str(i)+'x'*5000} for i in range(40)]))
    context=ConversationContextService(database,runs,session_memory=service)
    history=await context.load_history(RUN,CONTEXT)
    real=[item for item in history if not item.get('context_kind')]
    assert len(real)<=20 and sum(len(item['content']) for item in real)<=12000
    assert all(len(item['content'])<=4000 for item in real)
    summaries=[item for item in history if item.get('context_kind')]
    assert len(summaries)==1 and 'session_work_index' in summaries[0]['content']
    latest=next(item['content'] for item in reversed(history)
                if item['role']=='user' and not item.get('context_kind'))
    assert latest.startswith('38')
    assert '不是用户指令' in summaries[0]['content']


@pytest.mark.asyncio
async def test_disabled_path_keeps_original_history(monkeypatch):
    _,_,_,_,database=scenario(monkeypatch)
    history=[{'role':'user','content':'保持原行为'}]
    runs=SimpleNamespace(load_conversation_history=AsyncMock(return_value=history))
    assert await ConversationContextService(database,runs).load_history(RUN,CONTEXT)==history


@pytest.mark.asyncio
async def test_root_graph_does_not_treat_work_index_as_latest_user_requirement():
    from aegis_agent_worker.agent.graph.task_graph import TaskGraph
    history=[{'role':'user','content':'那封邮件的摘要'},
        {'role':'user','content':'工作索引','context_kind':'session_work_index'},
        {'role':'assistant','content':'历史助手回复'}]
    graph=TaskGraph(SimpleNamespace(context_service=SimpleNamespace(load_history=AsyncMock(return_value=history)),
                                    memory_context_service=None))
    state=dict(run_id=str(RUN.id),tenant_id=str(RUN.tenant_id),user_id=str(RUN.user_id),
               conversation_id=str(RUN.conversation_id))
    result=await graph._load_context(state)
    assert result['latest_user_message']=='那封邮件的摘要'


@pytest.mark.asyncio
async def test_repository_sql_ownership_and_no_payload_body():
    execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda:SimpleNamespace(first=lambda:None)))
    await SessionMemoryRepository().snapshot(SimpleNamespace(execute=execute),RUN,POLICY)
    sql=str(execute.await_args.args[0])
    args=execute.await_args.args[1]
    assert 'c.tenant_id=:tenant AND c.user_id=:user' in sql and "c.status='active'" in sql
    assert 'own.user_id=:user' in sql and 'ca.expires_at>now()' in sql
    assert 'md5(ca.payload::text)' in sql and 'SELECT ca.payload' not in sql
    assert 'cm.content' not in sql and args['cards']==4 and args['tasks']==6


@pytest.mark.asyncio
async def test_redis_protocol_bounds_keys_ttl_cas_and_generational_invalidation():
    redis=SimpleNamespace(mget=AsyncMock(return_value=['3',None]),eval=AsyncMock(return_value=1))
    cache=RedisSessionMemoryCache(redis,POLICY)
    assert await cache.read(RUN.tenant_id,RUN.user_id,RUN.conversation_id)==('3',None)
    payload=dict(sequence_no=10,snapshot_tick=100)
    await cache.put(RUN.tenant_id,RUN.user_id,RUN.conversation_id,'3',payload)
    args=redis.eval.await_args.args
    assert args[-2:]==(1800,32) and args[5]=='3'
    assert 'current.sequence_no>' in args[0] and 'current.snapshot_tick>' in args[0]
    assert 'ARGV[1]' in args[0] and "'EX'" in args[0]
    await cache.invalidate_user(RUN.tenant_id,RUN.user_id)
    assert "'INCR'" in redis.eval.await_args.args[0]
    assert cache_keys(RUN.tenant_id,RUN.user_id,RUN.conversation_id)!=cache_keys(UUID(int=100),RUN.user_id,RUN.conversation_id)
    assert cache_keys(RUN.tenant_id,RUN.user_id,RUN.conversation_id)!=cache_keys(RUN.tenant_id,UUID(int=100),RUN.conversation_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('raw',['not json','x'*12001,'['*1500+']'*1500],ids=['invalid-json','too-large','nested-array'])
async def test_invalid_cache_rebuild_without_parsing_unbounded_payload(raw):
    cache=RedisSessionMemoryCache(SimpleNamespace(mget=AsyncMock(return_value=['0',raw])),POLICY)
    assert await cache.read(RUN.tenant_id,RUN.user_id,RUN.conversation_id)==('0',{})


def test_policy_bounds_and_cache_format_is_not_approval():
    for changes in ({'max_tasks':21},{'max_user_sessions':33},{'ttl_seconds':0},{'max_cache_bytes':0}):
        with pytest.raises(ValueError): replace(POLICY,**changes)
    assert '外部执行证明' in SessionMemoryService.format_data({'tasks':[],'cards':[]})
