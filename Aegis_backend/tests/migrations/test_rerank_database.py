"""024 and actual atomic reserve SQL; opt-in, transactional temporary schema."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import pytest
from test_shadow_generation_database import database
from aegis_agent_worker.repository.execution_repository import ExecutionRepository

pytestmark=pytest.mark.skipif(os.getenv('AEGIS_GENERATION_DB_TESTS')!='1',reason='Opt-in local temporary schema')


@pytest.mark.asyncio
async def test_dedicated_and_cumulative_budget_and_migration(database):
    conn,session,user,doc,ver,raw,identity=database
    import psycopg
    from psycopg import sql
    role=conn.execute('SHOW role').fetchone()['role']
    conn.execute('RESET ROLE')
    conn.execute('''CREATE TABLE agent_runs(id uuid PRIMARY KEY,tenant_id uuid,user_id uuid,status text)''')
    conn.execute('''CREATE TABLE run_execution_budgets(run_id uuid PRIMARY KEY,tenant_id uuid,user_id uuid,
      policy_version text,model_calls integer DEFAULT 0,tool_calls integer DEFAULT 0,active_ms bigint DEFAULT 0)''')
    conn.execute('''CREATE TABLE run_execution_attempts(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),run_id uuid,
      tenant_id uuid,user_id uuid,kind varchar(16) CHECK(kind IN ('model','tool')),label text,risk_flags jsonb,
      status text DEFAULT 'running',error_code text,finished_at timestamptz,purpose text,model_provider text,
      model_name text,prompt_tokens bigint,completion_tokens bigint,total_tokens bigint,memory_count integer,
      memory_policy_version text,memory_selection_reason text,memory_selection_details jsonb)''')
    path=Path(__file__).resolve().parents[2]/'migrations/versions/024_rerank_governance.py'
    spec=importlib.util.spec_from_file_location('rerank_migration',path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    with patch.object(mod,'op',SimpleNamespace(execute=conn.execute)): mod.upgrade()
    conn.execute("ALTER TABLE knowledge_documents ADD COLUMN enabled boolean DEFAULT true,ADD COLUMN visibility text DEFAULT 'private'")
    conn.execute('''CREATE TABLE knowledge_chunks(id uuid,document_id uuid,version_id uuid,tenant_id uuid,
      indexed_at timestamptz DEFAULT now(),content_hash text)''')
    assert conn.execute('SELECT external_rerank_allowed FROM knowledge_documents WHERE id=%s',(doc,)).fetchone()['external_rerank_allowed'] is False
    conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA {} TO {}').format(
        sql.Identifier(conn.execute('SELECT current_schema() AS s').fetchone()['s']),sql.Identifier(role)))
    conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
    run=SimpleNamespace(id=uuid4(),tenant_id=user.tenant_id,user_id=user.id)
    conn.execute("INSERT INTO agent_runs VALUES(%s,%s,%s,'running')",(run.id,user.tenant_id,user.id))
    conn.execute("INSERT INTO run_execution_budgets(run_id,tenant_id,user_id,policy_version) VALUES(%s,%s,%s,'rr1')",(run.id,user.tenant_id,user.id))
    repo=ExecutionRepository()
    params=dict(kind='rerank',maximum=2,policy_version='rr1',elapsed_ms=0,time_limit_ms=1000,total_model_maximum=2)
    assert await repo.reserve_call(session,run,**params)==0
    attempt=await repo.start_attempt(session,run,kind='rerank',label='qwen3-rerank')
    await repo.finish_attempt(session,attempt,status='succeeded',error_code=None,purpose='knowledge_rerank',
        rerank_metadata=dict(candidate_count=1),total_tokens=10)
    assert await repo.reserve_call(session,run,**params)==0
    assert await repo.reserve_call(session,run,**params) is None
    counts=conn.execute('SELECT model_calls,rerank_calls FROM run_execution_budgets').fetchone()
    assert counts==dict(model_calls=2,rerank_calls=2)
    from aegis_agent_worker.repository.knowledge_retrieval_repository import KnowledgeRetrievalRepository
    class Probe(KnowledgeRetrievalRepository):
        async def _rows(self,context,query,**params):
            from sqlalchemy import text
            return (await session.execute(text(query),dict(tenant=context.tenant_id,user=context.user_id,**params))).mappings().all()
    chunk=uuid4()
    conn.execute('INSERT INTO knowledge_chunks(id,document_id,version_id,tenant_id,content_hash) VALUES(%s,%s,%s,%s,%s)',(chunk,doc,ver,user.tenant_id,'a'*64))
    evidence=[SimpleNamespace(chunk_id=chunk,version_id=ver,content_hash='a'*64)]
    context=SimpleNamespace(tenant_id=user.tenant_id,user_id=user.id)
    probe=Probe(None)
    assert not await probe.rerank_authorized(context,evidence)
    conn.execute('UPDATE knowledge_documents SET external_rerank_allowed=true')
    assert await probe.rerank_authorized(context,evidence)
    assert not await probe.rerank_authorized(SimpleNamespace(tenant_id=user.tenant_id,user_id=uuid4()),evidence)
    conn.execute('UPDATE knowledge_documents SET enabled=false')
    assert not await probe.rerank_authorized(context,evidence)
    # Even if dedicated cap rises, common model cap still prevents another call.
    assert await repo.reserve_call(session,run,**{**params,'maximum':4}) is None
    conn.execute("UPDATE agent_runs SET status='cancelled'")
    assert await repo.reserve_call(session,run,**{**params,'maximum':4,'total_model_maximum':4}) is None
    with pytest.raises(psycopg.Error):
        with conn.transaction():
            with patch.object(mod,'op',SimpleNamespace(execute=conn.execute)): mod.downgrade()
