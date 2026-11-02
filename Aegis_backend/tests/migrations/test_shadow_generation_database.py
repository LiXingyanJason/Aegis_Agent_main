"""Opt-in local PostgreSQL: actual 023 SQL/control plane; transaction rollback."""
import importlib.util
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import uuid4,UUID
import pytest

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'aegis_agent_worker/src'))
from aegis_agent_worker.service.knowledge.shadow_generation_service import ShadowGenerationService
from aegis_agent_worker.harness.exceptions import HarnessError

pytestmark=pytest.mark.skipif(os.getenv('AEGIS_GENERATION_DB_TESTS')!='1',reason='Opt-in local transactional PostgreSQL test')


@pytest.fixture
def database():
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from app.config.settings import get_settings
    url=get_settings().database_async_url.replace('postgresql+asyncpg://','postgresql://')
    assert urlparse(url).hostname in {'127.0.0.1','localhost','::1'}
    conn=psycopg.connect(url,connect_timeout=5,row_factory=dict_row)
    schema='aegis_gen_test_'+uuid4().hex;role='aegis_gen_role_'+uuid4().hex
    user=SimpleNamespace(id=uuid4(),tenant_id=uuid4());doc,ver=uuid4(),uuid4()
    raw='# 文档\n## 第三条\n原始条件。\n\n例外说明。'.encode()
    from hashlib import sha256
    try:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        conn.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
        conn.execute("SET LOCAL statement_timeout='5s'")
        conn.execute('''CREATE TABLE knowledge_documents(id uuid PRIMARY KEY,tenant_id uuid,user_id uuid,
          title text,deleted_at timestamptz,current_version_id uuid,is_sensitive boolean DEFAULT false,
          external_embedding_allowed boolean DEFAULT true)''')
        conn.execute('''CREATE TABLE knowledge_document_versions(id uuid,document_id uuid,tenant_id uuid,
          content_hash text,status text,PRIMARY KEY(id,document_id,tenant_id))''')
        for setting,name in (('app.tenant_id','aegis_current_tenant_id'),('app.user_id','aegis_current_user_id')):
            conn.execute(f"CREATE FUNCTION {name}() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT NULLIF(current_setting('{setting}',true),'')::uuid $$")
        conn.execute('INSERT INTO knowledge_documents(id,tenant_id,user_id,title,current_version_id) VALUES(%s,%s,%s,%s,%s)',(doc,user.tenant_id,user.id,'虚构标题',ver))
        conn.execute("INSERT INTO knowledge_document_versions VALUES(%s,%s,%s,%s,'ready')",(ver,doc,user.tenant_id,sha256(raw).hexdigest()))
        path=ROOT/'Aegis_backend/migrations/versions/023_shadow_index_generations.py'
        spec=importlib.util.spec_from_file_location('generation_migration',path)
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        with patch.object(mod,'op',SimpleNamespace(execute=conn.execute)): mod.upgrade()
        conn.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
        conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
        conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
        def identity(actor):
            conn.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.user_id',%s,true)",(str(actor.tenant_id),str(actor.id)))
        identity(user)
        async def execute(statement,params=None):
            query=re.sub(r'(?<!:):(\w+)',lambda m:'%('+m[1]+')s',str(statement))
            cur=conn.execute(query,params or {});rows=cur.fetchall() if cur.description else []
            return SimpleNamespace(mappings=lambda:SimpleNamespace(first=lambda:rows[0] if rows else None,all=lambda:rows),first=lambda:rows[0] if rows else None)
        yield conn,SimpleNamespace(execute=execute),user,doc,ver,raw,identity
    finally:
        conn.rollback()
        assert conn.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,)).fetchone() is None
        assert conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone() is None
        conn.close()


@pytest.mark.asyncio
async def test_prepare_validation_cas_rollback_rls_and_revocation(database):
    conn,session,user,doc,ver,raw,identity=database
    svc=ShadowGenerationService()
    a=await svc.prepare(session,user,doc,ver,'build-a',raw)
    b=await svc.prepare(session,user,doc,ver,'build-b',raw,dict(title_enhancement=True))
    assert (await svc.prepare(session,user,doc,ver,'build-a',raw))['reused']
    with pytest.raises(HarnessError): await svc.select(session,user,doc,ver,a['generation_id'],0)
    for item in (a,b):
        await svc.validate(session,user,doc,ver,item['generation_id'],item['manifest_hash'],'offline-review-only')
    one=await svc.select(session,user,doc,ver,a['generation_id'],0)
    assert one['revision']==1
    with pytest.raises(HarnessError): await svc.select(session,user,doc,ver,b['generation_id'],0)
    assert (await svc.select(session,user,doc,ver,b['generation_id'],1))['revision']==2
    assert (await svc.select(session,user,doc,ver,a['generation_id'],2,rollback=True))['revision']==3
    assert (await svc.select(session,user,doc,ver,None,3,rollback=True))['revision']==4
    old=await svc.repo.snapshot(session,user,doc,ver,a['generation_id'])
    assert old['manifest_hash']==a['manifest_hash']
    # Old document pointer and contents are untouched throughout.
    assert conn.execute('SELECT current_version_id FROM knowledge_documents WHERE id=%s',(doc,)).fetchone()['current_version_id']==ver
    import psycopg
    with pytest.raises(psycopg.Error):
        with conn.transaction():
            conn.execute("UPDATE knowledge_generation_chunks SET payload='{}' WHERE generation_id=%s",(a['generation_id'],))
    with pytest.raises(psycopg.Error):
        with conn.transaction():
            conn.execute('''INSERT INTO knowledge_generation_chunks(generation_id,tenant_id,user_id,document_id,version_id,chunk_id,position,payload)
              VALUES(%s,%s,%s,%s,%s,%s,999,'{}')''',(a['generation_id'],user.tenant_id,user.id,doc,ver,uuid4()))
    with pytest.raises(psycopg.Error):
        with conn.transaction():
            conn.execute("UPDATE knowledge_index_generations SET source_hash=%s WHERE id=%s",('0'*64,a['generation_id']))
    conn.execute('UPDATE knowledge_document_versions SET content_hash=%s WHERE id=%s',('0'*64,ver))
    with pytest.raises(HarnessError): await svc.repo.snapshot(session,user,doc,ver,a['generation_id'])
    from hashlib import sha256
    conn.execute('UPDATE knowledge_document_versions SET content_hash=%s WHERE id=%s',(sha256(raw).hexdigest(),ver))
    for actor in (SimpleNamespace(id=uuid4(),tenant_id=user.tenant_id),SimpleNamespace(id=uuid4(),tenant_id=uuid4())):
        identity(actor)
        assert conn.execute('SELECT count(*) AS n FROM knowledge_index_generations').fetchone()['n']==0
        with pytest.raises(HarnessError): await svc.repo.snapshot(session,actor,doc,ver,a['generation_id'])
    identity(user)
    conn.execute('UPDATE knowledge_documents SET external_embedding_allowed=false WHERE id=%s',(doc,))
    with pytest.raises(HarnessError): await svc.select(session,user,doc,ver,a['generation_id'],4)
    with pytest.raises(HarnessError): await svc.repo.snapshot(session,user,doc,ver,a['generation_id'])
