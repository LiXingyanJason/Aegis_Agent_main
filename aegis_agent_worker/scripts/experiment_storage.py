"""Owned random-schema storage for experiments; never reads business tables."""
from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from urllib.parse import urlparse

import psycopg
from psycopg import sql
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

SUPPORT = '''
CREATE TABLE tenants(id uuid PRIMARY KEY);
CREATE TABLE users(id uuid PRIMARY KEY,tenant_id uuid NOT NULL);
CREATE TABLE agent_runs(id uuid PRIMARY KEY,tenant_id uuid,user_id uuid,status text);
CREATE TABLE run_execution_budgets(run_id uuid PRIMARY KEY,tenant_id uuid,user_id uuid,
 policy_version text,model_calls int DEFAULT 0,tool_calls int DEFAULT 0,active_ms bigint DEFAULT 0);
CREATE TABLE run_execution_attempts(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),run_id uuid,
 tenant_id uuid,user_id uuid,kind text,label text,risk_flags jsonb,status text DEFAULT 'running',
 error_code text,purpose text,model_provider text,model_name text,prompt_tokens bigint,
 completion_tokens bigint,total_tokens bigint,memory_count int,memory_policy_version text,
 memory_selection_reason text,memory_selection_details jsonb,started_at timestamptz DEFAULT now(),
 finished_at timestamptz,UNIQUE(id,run_id,tenant_id));
CREATE FUNCTION aegis_current_tenant_id() RETURNS uuid LANGUAGE sql STABLE AS $$
 SELECT NULLIF(current_setting('app.tenant_id',true),'')::uuid $$;
CREATE FUNCTION aegis_current_user_id() RETURNS uuid LANGUAGE sql STABLE AS $$
 SELECT NULLIF(current_setting('app.user_id',true),'')::uuid $$;
'''


class ExperimentStorage:
    def __init__(self, settings, prefix):
        assert prefix in ('e08','e09')
        self.schema = 'aegis_'+prefix+'_'+uuid4().hex
        self.url = settings.database_async_url.replace('postgresql+asyncpg://','postgresql://')
        assert urlparse(self.url).hostname in ('localhost','127.0.0.1','::1')
        self.settings = settings

    async def create(self):
        backend = Path(__file__).resolve().parents[2]/'Aegis_backend'
        sys.path.insert(0,str(backend))
        with psycopg.connect(self.url,connect_timeout=5) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(self.schema)))
            conn.execute(SUPPORT)
            spec=importlib.util.spec_from_file_location('experiment_skill_migration',backend/'migrations/versions/022_skills.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            with patch.object(module,'op',SimpleNamespace(execute=conn.execute)):
                module.upgrade()
        self.engine=create_async_engine(self.settings.database_async_url,hide_parameters=True,
            connect_args={'server_settings':{'search_path':self.schema+',pg_catalog','statement_timeout':'10000'}})
        self.factory=async_sessionmaker(self.engine,expire_on_commit=False)

    @asynccontextmanager
    async def session(self):
        async with self.factory() as session:
            yield session

    async def cleanup(self):
        await self.engine.dispose()
        assert re.fullmatch(r'aegis_e0[89]_[a-f0-9]{32}',self.schema)
        with psycopg.connect(self.url,connect_timeout=5) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
            assert conn.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(self.schema,)).fetchone() is None
