"""显式选择的真实PG测试：临时schema/角色均在事务结束回滚，不动业务表。"""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skipif(os.getenv("AEGIS_KNOWLEDGE_DB_TESTS") != "1",
    reason="设置 AEGIS_KNOWLEDGE_DB_TESTS=1 才在本地PG上创建可回滚测试schema/角色")


def migration(number):
    directory = Path(__file__).resolve().parents[2]/"migrations/versions"
    path = next(directory.glob(f"{number}_*.py"))
    spec = importlib.util.spec_from_file_location(f"kb_migration_{number}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply(connection, number, direction="upgrade"):
    module = migration(number)
    with patch.object(module, "op", SimpleNamespace(execute=connection.execute)):
        getattr(module, direction)()


@pytest.fixture
def database():
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.rows import dict_row
    from app.config.settings import get_settings
    connection = psycopg.connect(get_settings().database_async_url.replace("postgresql+asyncpg://", "postgresql://"),
                                 connect_timeout=5, row_factory=dict_row)
    schema, role = "aegis_kb_test_"+uuid4().hex, "aegis_kb_role_"+uuid4().hex
    try:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(role)))
        connection.execute(sql.SQL("SET LOCAL search_path TO {},public").format(sql.Identifier(schema)))
        connection.execute("CREATE TABLE tenants(id uuid PRIMARY KEY)")
        connection.execute("CREATE TABLE users(id uuid PRIMARY KEY,tenant_id uuid NOT NULL REFERENCES tenants)")
        connection.execute("CREATE TABLE agent_runs(id uuid PRIMARY KEY,tenant_id uuid,user_id uuid,status text)")
        for name, setting in (("aegis_current_tenant_id", "app.tenant_id"), ("aegis_current_user_id", "app.user_id")):
            connection.execute(f"CREATE FUNCTION {name}() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT NULLIF(current_setting('{setting}',true),'')::uuid $$")
        apply(connection, "010")
        apply(connection, "011")
        connection.execute("INSERT INTO tenants VALUES (%s),(%s)", (UUID(int=1), UUID(int=2)))
        connection.execute("INSERT INTO users VALUES (%s,%s),(%s,%s),(%s,%s)",
            (UUID(int=10), UUID(int=1), UUID(int=11), UUID(int=1), UUID(int=20), UUID(int=2)))
        connection.execute("INSERT INTO agent_runs VALUES (%s,%s,%s,'running')", (UUID(int=600), UUID(int=1), UUID(int=10)))
        for table in ("tenants", "users", "agent_runs", "knowledge_documents", "knowledge_document_versions",
                      "knowledge_chunks", "knowledge_index_jobs", "embedding_execution_budgets", "embedding_execution_attempts"):
            connection.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON {} TO {}").format(sql.Identifier(table), sql.Identifier(role)))
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        connection.execute(sql.SQL("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        yield connection
    finally:
        connection.rollback()
        connection.close()


def identity(c, tenant=1, user=10):
    c.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.user_id',%s,true)",
              (str(UUID(int=tenant)), str(UUID(int=user))))


def seed(c, document=100, tenant=1, user=10, shared=False):
    identity(c, tenant, user)
    c.execute("INSERT INTO knowledge_documents(id,tenant_id,user_id,title,category) VALUES(%s,%s,%s,'虚构政策','policy')",
              (UUID(int=document), UUID(int=tenant), UUID(int=user)))
    c.execute("""INSERT INTO knowledge_document_versions(id,document_id,tenant_id,version_no,version_label,source_text,
        content_hash,status,embedding_provider,embedding_model,embedding_model_version,embedding_dimensions)
        VALUES(%s,%s,%s,1,'v1','虚构资料',%s,'ready','openai_compatible','test-model','test-v1',3)""",
        (UUID(int=document+1), UUID(int=document), UUID(int=tenant), "a"*64))
    c.execute("UPDATE knowledge_documents SET current_version_id=%s,visibility=%s WHERE id=%s",
              (UUID(int=document+1), "tenant" if shared else "private", UUID(int=document)))
    c.execute("""INSERT INTO knowledge_chunks(id,document_id,version_id,tenant_id,position,content,start_offset,end_offset,
        content_hash,embedding_provider,embedding_model,embedding_model_version,embedding_dimensions)
        VALUES(%s,%s,%s,%s,0,'虚构资料',0,4,%s,'openai_compatible','test-model','test-v1',3)""",
        (UUID(int=document+2), UUID(int=document), UUID(int=document+1), UUID(int=tenant), "b"*64))


def test_actual_rls_private_shared_cross_tenant_and_no_context(database):
    c = database
    seed(c)
    seed(c, document=200, shared=True)
    seed(c, document=300, tenant=2, user=20, shared=True)
    for tenant, user, expected in ((1,10,{100,200}), (1,11,{200}), (2,20,{300})):
        identity(c, tenant, user)
        assert {row["id"].int for row in c.execute("SELECT id FROM knowledge_documents")} == expected
        assert {row["document_id"].int for row in c.execute("SELECT document_id FROM knowledge_chunks")} == expected
        assert {row["document_id"].int for row in c.execute("SELECT document_id FROM knowledge_document_versions")} == expected
    c.execute("SELECT set_config('app.tenant_id','',true),set_config('app.user_id','',true)")
    assert not c.execute("SELECT id FROM knowledge_chunks").fetchall()


def test_shared_reader_cannot_edit_and_revocation_is_immediate(database):
    seed(database, shared=True)
    identity(database, user=11)
    assert database.execute("UPDATE knowledge_documents SET title='修改' RETURNING id").rowcount == 0
    assert database.execute("UPDATE knowledge_document_versions SET source_text='修改' RETURNING id").rowcount == 0
    identity(database)
    database.execute("UPDATE knowledge_documents SET visibility='private'")
    identity(database, user=11)
    assert not database.execute("SELECT id FROM knowledge_chunks").fetchall()


def test_visibility_defaults_private_and_model_profile_is_enforced(database):
    seed(database)
    assert database.execute("SELECT visibility FROM knowledge_documents").fetchone()["visibility"] == "private"
    with pytest.raises(Exception), database.transaction():
        database.execute("UPDATE knowledge_chunks SET embedding_dimensions=4")
    assert database.execute("SELECT embedding_dimensions FROM knowledge_chunks").fetchone()["embedding_dimensions"] == 3


@pytest.mark.asyncio
async def test_actual_index_subject_requires_explicit_data_authorization(database):
    from sqlalchemy.dialects.postgresql import dialect
    from aegis_agent_worker.repository.embedding_repository import EmbeddingRepository
    from aegis_agent_worker.harness.embedding_executor import EmbeddingSubject
    from aegis_agent_worker.harness.exceptions import HarnessError
    from aegis_agent_worker.embedding.contracts import EmbeddingProfile
    seed(database)
    database.execute("UPDATE knowledge_document_versions SET status='pending'")
    database.execute("INSERT INTO knowledge_index_jobs(id,tenant_id,user_id,document_id,version_id) VALUES(%s,%s,%s,%s,%s)",
        (UUID(int=500), UUID(int=1), UUID(int=10), UUID(int=100), UUID(int=101)))
    class Session:
        async def execute(self, statement, params):
            cursor = database.execute(str(statement.compile(dialect=dialect(paramstyle="pyformat"))), params)
            return SimpleNamespace(first=cursor.fetchone)
    repository, session = EmbeddingRepository(), Session()
    subject = EmbeddingSubject("job", UUID(int=500), UUID(int=1), UUID(int=10))
    profile = EmbeddingProfile("openai_compatible", "test-model", "test-v1", 3)
    with pytest.raises(HarnessError):
        await repository.authorize(session, subject, profile)
    database.execute("UPDATE knowledge_documents SET external_embedding_allowed=true")
    await repository.authorize(session, subject, profile)
    database.execute("UPDATE knowledge_documents SET is_sensitive=true")
    with pytest.raises(HarnessError):
        await repository.authorize(session, subject, profile)


@pytest.mark.asyncio
async def test_repository_filters_also_protect_when_runtime_role_bypasses_rls(database):
    from app.repository.knowledge_repository import KnowledgeRepository
    from sqlalchemy.dialects.postgresql import dialect
    seed(database)
    seed(database, document=200, shared=True)
    seed(database, document=300, tenant=2, user=20, shared=True)
    database.execute("RESET ROLE")
    class Session:
        async def execute(self, statement, params):
            cursor = database.execute(str(statement.compile(dialect=dialect(paramstyle="pyformat"))), params)
            return SimpleNamespace(mappings=lambda: SimpleNamespace(first=cursor.fetchone), first=cursor.fetchone)
    repository, session = KnowledgeRepository(), Session()
    async def read(doc, tenant=1, user=11):
        return await repository.get_document(session, tenant_id=UUID(int=tenant), user_id=UUID(int=user), document_id=UUID(int=doc))
    assert await read(100) is None
    assert await read(200) is not None
    assert await read(300) is None
    assert not await repository.set_visibility(session, tenant_id=UUID(int=1), user_id=UUID(int=11),
                                              document_id=UUID(int=100), visibility="tenant")


def test_upgrade_downgrade_roundtrip_without_pgvector_dependency(database):
    database.execute("RESET ROLE")
    apply(database, "011", "downgrade")
    apply(database, "010", "downgrade")
    apply(database, "010")
    apply(database, "011")
    assert database.execute("SELECT count(*) AS n FROM knowledge_documents").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_actual_embedding_repository_budget_and_subject_permissions(database):
    from sqlalchemy.dialects.postgresql import dialect
    from aegis_agent_worker.repository.embedding_repository import EmbeddingRepository
    from aegis_agent_worker.harness.embedding_executor import EmbeddingSubject
    from aegis_agent_worker.harness.policies.embedding import QUERY_POLICY
    from aegis_agent_worker.harness.exceptions import HarnessError
    from aegis_agent_worker.embedding.contracts import EmbeddingProfile
    class Result:
        def __init__(self, rows): self.rows = rows
        def first(self): return self.rows[0] if self.rows else None
        def mappings(self): return self
        def one(self): assert len(self.rows)==1; return self.rows[0]
        def scalar_one(self): return next(iter(self.one().values()))
    class Session:
        async def execute(self, statement, params):
            compiled = statement.compile(dialect=dialect(paramstyle="pyformat"))
            cursor = database.execute(str(compiled), params)
            return Result(cursor.fetchall() if cursor.description else [])
    identity(database)
    repository, session = EmbeddingRepository(), Session()
    subject = EmbeddingSubject("run", UUID(int=600), UUID(int=1), UUID(int=10))
    profile = EmbeddingProfile("openai_compatible", "test-model", "test-v1", 3)
    await repository.authorize(session, subject, profile)
    budget = await repository.ensure_budget(session, subject, profile, QUERY_POLICY.version)
    for _ in range(QUERY_POLICY.max_calls):
        await repository.reserve(session, budget["id"], subject, QUERY_POLICY, 10)
    with pytest.raises(HarnessError, match="预算已耗尽"):
        await repository.reserve(session, budget["id"], subject, QUERY_POLICY, 10)
    changed = EmbeddingProfile("openai_compatible", "different", "test-v1", 3)
    with pytest.raises(HarnessError, match="已改变"):
        await repository.ensure_budget(session, subject, changed, QUERY_POLICY.version)
    identity(database, user=11)
    with pytest.raises(HarnessError, match="不可访问"):
        await repository.authorize(session, EmbeddingSubject("run", UUID(int=600), UUID(int=1), UUID(int=11)), profile)
