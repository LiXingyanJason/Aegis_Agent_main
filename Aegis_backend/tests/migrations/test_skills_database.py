"""022 on a random schema and restricted role; everything is rolled back.

Explicit opt-in: AEGIS_SKILL_DB_TESTS=1. No business tables or external models.
"""
import importlib.util
import os
from pathlib import Path
import re
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException
from app.param.skill_param import SkillContent,SkillUpdate,SkillRevision,SkillEnabled
from app.service.skill_service import SkillService

pytestmark=pytest.mark.skipif(os.getenv('AEGIS_SKILL_DB_TESTS')!='1',reason='Opt-in isolated PostgreSQL test')

@pytest.fixture
def database():
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from app.config.settings import get_settings
    url=get_settings().database_async_url.replace('postgresql+asyncpg://','postgresql://')
    assert urlparse(url).hostname in {'localhost','127.0.0.1','::1'},'This test only permits local development PostgreSQL'
    conn=psycopg.connect(url,connect_timeout=5,row_factory=dict_row)
    schema='aegis_skill_test_'+uuid4().hex
    role='aegis_skill_role_'+uuid4().hex
    user=SimpleNamespace(id=uuid4(),tenant_id=uuid4())
    same_tenant=SimpleNamespace(id=uuid4(),tenant_id=user.tenant_id)
    other=SimpleNamespace(id=uuid4(),tenant_id=uuid4())
    run=SimpleNamespace(id=uuid4(),tenant_id=user.tenant_id,user_id=user.id)
    try:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        conn.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
        conn.execute("SET LOCAL statement_timeout='5s'")
        conn.execute('CREATE TABLE tenants(id uuid PRIMARY KEY)')
        conn.execute('CREATE TABLE users(id uuid PRIMARY KEY,tenant_id uuid NOT NULL)')
        conn.execute('CREATE TABLE agent_runs(id uuid PRIMARY KEY,tenant_id uuid,user_id uuid,status text)')
        conn.execute('''CREATE TABLE run_execution_attempts(id uuid PRIMARY KEY,run_id uuid,tenant_id uuid,
            status text DEFAULT 'running',prompt_tokens bigint,completion_tokens bigint,total_tokens bigint,
            started_at timestamptz DEFAULT now(),finished_at timestamptz,UNIQUE(id,run_id,tenant_id))''')
        for setting,name in (('app.tenant_id','aegis_current_tenant_id'),('app.user_id','aegis_current_user_id')):
            conn.execute(f"CREATE FUNCTION {name}() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT NULLIF(current_setting('{setting}',true),'')::uuid $$")
        conn.execute('INSERT INTO tenants VALUES(%s),(%s)',(user.tenant_id,other.tenant_id))
        for actor in (user,same_tenant,other):
            conn.execute('INSERT INTO users VALUES(%s,%s)',(actor.id,actor.tenant_id))
        conn.execute("INSERT INTO agent_runs VALUES(%s,%s,%s,'running')",(run.id,user.tenant_id,user.id))
        path=Path(__file__).resolve().parents[2]/'migrations/versions/022_skills.py'
        spec=importlib.util.spec_from_file_location('skill_migration',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module,'op',SimpleNamespace(execute=conn.execute)): module.upgrade()
        conn.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
        conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
        conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
        def identity(actor):
            conn.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.user_id',%s,true)",(str(actor.tenant_id),str(actor.id)))
        identity(user)
        async def execute(statement,params=None):
            query=re.sub(r'(?<!:):(\w+)',lambda m:'%('+m[1]+')s',str(statement))
            cursor=conn.execute(query,params or {})
            rows=cursor.fetchall() if cursor.description else []
            return SimpleNamespace(mappings=lambda:SimpleNamespace(first=lambda:rows[0] if rows else None,
                one=lambda:rows[0],all=lambda:rows),first=lambda:rows[0] if rows else None,
                scalar_one=lambda:next(iter(rows[0].values())))
        yield conn,SimpleNamespace(execute=execute),user,same_tenant,other,run,identity
    finally:
        conn.rollback()
        # The schema and role were never committed, including on assertion failure.
        assert conn.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,)).fetchone() is None
        assert conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone() is None
        conn.close()

@pytest.mark.asyncio
async def test_lifecycle_rls_pinning_and_usage_with_real_sql(database):
    conn,session,user,same,other,run,identity=database
    service=SkillService()
    content=SkillContent(**service.template())
    draft=await service.create(session,user,content)
    assert draft['revision']==1 and not draft['enabled'] and draft['published_version'] is None
    worker_src=Path(__file__).resolve().parents[3]/'aegis_agent_worker/src'
    sys.path.insert(0,str(worker_src))
    from aegis_agent_worker.repository.skill_repository import SkillRepository
    class WorkerRepository(SkillRepository):
        @asynccontextmanager
        async def session(self,run): yield session
    reader=WorkerRepository(None)
    purpose='mail_reply_draft'
    assert await reader.candidates(run,purpose,20)==[]
    published=await service.change(session,user,draft['id'],SkillRevision(expected_revision=1),'published')
    assert published['published_version']==1 and published['revision']==2 and published['enabled']
    candidates=await reader.candidates(run,purpose,20)
    assert len(candidates)==1
    claimed,binding=await reader.claim_selection(run,purpose)
    assert claimed and binding['status']=='selecting'
    assert not (await reader.claim_selection(run,purpose))[0]
    await reader.select(run,purpose,candidates[0],'selected')
    assert await reader.claim_read(run,purpose,draft['id'])
    assert not await reader.claim_read(run,purpose,draft['id'])
    loaded=await reader.load(run,purpose)
    assert loaded['version']==1 and loaded['reads']==1
    attempt=uuid4()
    conn.execute('INSERT INTO run_execution_attempts(id,run_id,tenant_id) VALUES(%s,%s,%s)',(attempt,run.id,user.tenant_id))
    await reader.record(run,purpose,attempt,loaded)
    assert conn.execute('SELECT count(*) AS n FROM run_skill_usages').fetchone()['n']==1
    usage=await service.usages(session,user,run.id,50,None)
    assert usage['bindings'][0]['status']=='selected'
    assert usage['items'][0]['total_tokens'] is None and usage['items'][0]['duration_ms'] is None
    assert 'body' not in usage['items'][0]
    assert (await service.usages(session,user,run.id,50,attempt))['items']==[]
    for actor in (same,other):
        identity(actor)
        assert conn.execute('SELECT * FROM skills').fetchall()==[]
        assert conn.execute('SELECT * FROM skill_versions').fetchall()==[]
        assert conn.execute('SELECT * FROM run_skill_usages').fetchall()==[]
        with pytest.raises(HTTPException) as error: await service.owned(session,actor,draft['id'])
        assert error.value.status_code==404
        with pytest.raises(HTTPException) as error: await service.usages(session,actor,run.id,50,None)
        assert error.value.status_code==404
    identity(user)
    edit=SkillUpdate(**{**content.model_dump(),'body':'新版模板'},expected_revision=2)
    updated=await service.change(session,user,draft['id'],edit,'edited')
    assert updated['published_version']==1 and (await reader.load(run,purpose))['body']==content.body
    with pytest.raises(HTTPException) as error:
        await service.change(session,user,draft['id'],SkillRevision(expected_revision=2),'published')
    assert error.value.status_code==409
    await service.change(session,user,draft['id'],SkillRevision(expected_revision=3),'published')
    assert (await reader.candidates(run,purpose,20))[0]['version']==3
    assert (await reader.load(run,purpose))['version']==1
    assert len(await service.repo.versions(session,user,draft['id'],20,None))==2
    with pytest.raises(Exception,match='immutable'),conn.transaction():
        conn.execute("UPDATE skill_versions SET body='changed' WHERE skill_id=%s",(draft['id'],))
    await service.change(session,user,draft['id'],SkillEnabled(expected_revision=4,enabled=False),'enabled')
    assert await reader.load(run,purpose) is None
    await service.change(session,user,draft['id'],SkillEnabled(expected_revision=5,enabled=True),'enabled')
    await service.change(session,user,draft['id'],SkillRevision(expected_revision=6),'deleted')
    assert await reader.load(run,purpose) is None and await reader.candidates(run,purpose,20)==[]
    assert conn.execute('SELECT count(*) AS n FROM skill_events').fetchone()['n']==7
