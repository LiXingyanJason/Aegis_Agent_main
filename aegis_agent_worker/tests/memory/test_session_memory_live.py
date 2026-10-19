"""显式本地集成：随机Redis键/回滚PG schema，不读取或修改现有业务资料。"""
import asyncio
from dataclasses import replace
import os
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aegis_agent_worker.config.settings import get_settings
from aegis_agent_worker.harness.policy_provider import DEFAULT_POLICIES
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.repository.session_memory_repository import SessionMemoryRepository
from aegis_agent_worker.storage.session_memory_cache import RedisSessionMemoryCache,cache_keys


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('AEGIS_SESSION_REDIS_TESTS')!='1',reason='显式启用本地随机Redis键测试')
async def test_real_lua_cas_ttl_eviction_and_logout_write_fence():
    from redis.asyncio import Redis
    from urllib.parse import urlparse
    settings=get_settings()
    assert urlparse(settings.redis_url).hostname in {'localhost','127.0.0.1','::1'}
    redis=Redis.from_url(settings.redis_url,decode_responses=True)
    tenant,user=uuid4(),uuid4()
    conversations=[uuid4() for _ in range(3)]
    policy=replace(DEFAULT_POLICIES.current().session_memory,max_user_sessions=2)
    cache=RedisSessionMemoryCache(redis,policy)
    epoch,index,_=cache_keys(tenant,user)
    keys=[cache_keys(tenant,user,c)[2] for c in conversations]
    unrelated='aegis_session_unrelated_test_'+uuid4().hex
    try:
        generation,_=await cache.read(tenant,user,conversations[0])
        payload=dict(generation=generation,sequence_no=10,snapshot_tick=100)
        assert await cache.put(tenant,user,conversations[0],generation,payload)==1
        assert 0 < await redis.ttl(keys[0]) <= policy.ttl_seconds
        assert await cache.put(tenant,user,conversations[0],generation,{**payload,'sequence_no':9,'snapshot_tick':200})==0
        assert await cache.put(tenant,user,conversations[0],generation,{**payload,'snapshot_tick':99})==0
        assert (await cache.read(tenant,user,conversations[0]))[1]==payload
        await cache.put(tenant,user,conversations[1],generation,{**payload,'snapshot_tick':101})
        await cache.put(tenant,user,conversations[2],generation,{**payload,'snapshot_tick':102})
        assert await redis.get(keys[0]) is None and await redis.zcard(index)==2
        assert await cache.invalidate_user(tenant,user)==2
        assert all(value is None for value in await redis.mget(keys))
        # 退出前读到的代际不能在清理后写回。
        assert await cache.put(tenant,user,conversations[0],generation,payload)==0
        fresh,_=await cache.read(tenant,user,conversations[0])
        assert fresh!=generation
        assert await cache.put(tenant,user,conversations[0],fresh,{**payload,'generation':fresh})==1
        await redis.pexpire(keys[0],1)
        await asyncio.sleep(0.02)
        assert (await cache.read(tenant,user,conversations[0]))[1] is None
        await redis.set(unrelated,'owned-test-sentinel',ex=10)
        await redis.zadd(index,{unrelated:0})
        await cache.invalidate_user(tenant,user)
        assert await redis.get(unrelated)=='owned-test-sentinel'
    finally:
        # 仅删除本测试生成的明确随机键，不使用SCAN或生产前缀。
        await redis.delete(epoch,index,*keys,unrelated)
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('AEGIS_SESSION_PG_TESTS')!='1',reason='显式启用可回滚PG临时schema测试')
async def test_real_sql_scope_version_and_expired_reference():
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    settings=get_settings()
    conn=psycopg.connect(settings.database_psycopg_url,connect_timeout=5,row_factory=dict_row)
    schema='aegis_session_test_'+uuid4().hex
    tenant,user,conversation,run,prior,card=[uuid4() for _ in range(6)]
    try:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        conn.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)))
        conn.execute('CREATE TABLE conversations(id uuid,tenant_id uuid,user_id uuid,status text)')
        conn.execute('CREATE TABLE conversation_messages(conversation_id uuid,tenant_id uuid,sequence_no int)')
        conn.execute('CREATE TABLE agent_runs(id uuid,tenant_id uuid,user_id uuid,conversation_id uuid,run_type text,status text,created_at timestamptz,updated_at timestamptz)')
        conn.execute('CREATE TABLE conversation_message_artifacts(id uuid,tenant_id uuid,user_id uuid,conversation_id uuid,run_id uuid,artifact_type text,payload jsonb,created_at timestamptz,expires_at timestamptz)')
        conn.execute("INSERT INTO conversations VALUES (%s,%s,%s,'active')",(conversation,tenant,user))
        conn.execute('INSERT INTO conversation_messages VALUES (%s,%s,5)',(conversation,tenant))
        for identifier in (run,prior):
            conn.execute("INSERT INTO agent_runs VALUES (%s,%s,%s,%s,'conversation','completed',now(),now())",(identifier,tenant,user,conversation))
        conn.execute("INSERT INTO conversation_message_artifacts VALUES (%s,%s,%s,%s,%s,'mail_candidates','{}',now(),now()+interval '1 day')",(card,tenant,user,conversation,prior))

        async def execute(statement,params):
            query=re.sub(r'(?<!:):(\w+)',lambda m:'%('+m[1]+')s',str(statement))
            row=conn.execute(query,params).fetchone()
            return SimpleNamespace(mappings=lambda:SimpleNamespace(first=lambda:row))

        repo=SessionMemoryRepository()
        session=SimpleNamespace(execute=execute)
        claimed=ClaimedRun(run,tenant,user,conversation)
        policy=DEFAULT_POLICIES.current().session_memory
        snapshot=await repo.snapshot(session,claimed,policy)
        assert snapshot['sequence_no']==5 and len(snapshot['cards'])==1 and len(snapshot['tasks'])==1
        assert snapshot['tasks'][0]['id']==str(prior)
        for wrong in (replace(claimed,tenant_id=uuid4()),replace(claimed,user_id=uuid4()),replace(claimed,conversation_id=uuid4()),replace(claimed,id=uuid4())):
            assert await repo.snapshot(session,wrong,policy) is None
        conn.execute('UPDATE conversation_message_artifacts SET payload=%s::jsonb',('{"changed":true}',))
        updated=await repo.snapshot(session,claimed,policy)
        assert updated['cards'][0]['source_version']!=snapshot['cards'][0]['source_version']
        conn.execute("UPDATE conversation_message_artifacts SET expires_at=now()-interval '1 second'")
        assert (await repo.snapshot(session,claimed,policy))['cards']==[]
        conn.execute("UPDATE conversations SET status='archived'")
        assert await repo.snapshot(session,claimed,policy) is None
    finally:
        conn.rollback()
        conn.close()
