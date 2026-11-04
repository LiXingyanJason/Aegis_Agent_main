"""Actual 027 SQL and settings CAS in an isolated rollback-only local schema."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import pytest
from test_shadow_generation_database import database

pytestmark=pytest.mark.skipif(os.getenv('AEGIS_GENERATION_DB_TESTS')!='1',reason='Opt-in isolated local PostgreSQL')


@pytest.mark.asyncio
async def test_settings_proposals_and_rls(database):
    from psycopg import sql,Error
    from fastapi import HTTPException
    from app.service.memory_formation_service import MemoryFormationService
    from app.param.memory_formation_param import MemoryFormationParam
    conn,session,user,_,_,_,identity=database
    role=conn.execute('SHOW role').fetchone()['role'];conn.execute('RESET ROLE')
    conn.execute('CREATE TABLE tenants(id uuid PRIMARY KEY)')
    conn.execute('CREATE TABLE users(id uuid PRIMARY KEY)')
    conn.execute('CREATE TABLE conversations(id uuid PRIMARY KEY,tenant_id uuid,user_id uuid)')
    conn.execute('CREATE TABLE memory_items(id uuid PRIMARY KEY)')
    conn.execute('INSERT INTO tenants VALUES(%s)',(user.tenant_id,))
    conn.execute('INSERT INTO users VALUES(%s)',(user.id,))
    conv=uuid4();conn.execute('INSERT INTO conversations VALUES(%s,%s,%s)',(conv,user.tenant_id,user.id))
    path=Path(__file__).resolve().parents[2]/'migrations/versions/027_memory_formation.py'
    spec=importlib.util.spec_from_file_location('formation_migration',path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    with patch.object(mod,'op',SimpleNamespace(execute=conn.execute)):mod.upgrade()
    schema=conn.execute('SELECT current_schema() AS s').fetchone()['s']
    conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
    conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
    service=MemoryFormationService()
    assert (await service.read(session,user))['version']==0
    settings=MemoryFormationParam(expected_version=0,mode='controlled_auto',auto_approval_consent=True,
        excluded_conversation_ids=[conv],use_existing=False)
    assert (await service.update(session,user,settings))['version']==1
    with pytest.raises(HTTPException) as e:await service.update(session,user,settings)
    assert e.value.status_code==409
    job=uuid4();candidate=uuid4()
    conn.execute("INSERT INTO memory_formation_jobs(id,tenant_id,user_id,conversation_id,idempotency_key,policy_version,extractor_version,settings_version,source_manifest) VALUES(%s,%s,%s,%s,'once','v1','v1',1,'[{}]')",(job,user.tenant_id,user.id,conv))
    conn.execute("INSERT INTO memory_formation_proposals(id,tenant_id,user_id,job_id,category,content,proposal) VALUES(%s,%s,%s,%s,'preference','虚构偏好','{}')",(candidate,user.tenant_id,user.id,job))
    conn.execute("UPDATE memory_formation_proposals SET status='rejected' WHERE id=%s",(candidate,))
    with pytest.raises(Error):
        with conn.transaction():conn.execute("UPDATE memory_formation_proposals SET status='pending' WHERE id=%s",(candidate,))
    with pytest.raises(Error):
        with conn.transaction():conn.execute("UPDATE memory_lifecycle_events SET reason='changed'")
    other=SimpleNamespace(id=uuid4(),tenant_id=user.tenant_id)
    identity(other)
    for table in ('memory_formation_jobs','memory_formation_proposals','memory_formation_settings','memory_lifecycle_events'):
        assert conn.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n']==0
    identity(user)
    assert conn.execute('SELECT count(*) AS n FROM memory_items').fetchone()['n']==0
