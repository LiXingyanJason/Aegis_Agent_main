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
        apply(connection, "012")
        apply(connection, "013")
        apply(connection, "015")
        apply(connection, "016")
        connection.execute("INSERT INTO tenants VALUES (%s),(%s)", (UUID(int=1), UUID(int=2)))
        connection.execute("INSERT INTO users VALUES (%s,%s),(%s,%s),(%s,%s)",
            (UUID(int=10), UUID(int=1), UUID(int=11), UUID(int=1), UUID(int=20), UUID(int=2)))
        connection.execute("INSERT INTO agent_runs VALUES (%s,%s,%s,'running')", (UUID(int=600), UUID(int=1), UUID(int=10)))
        for table in ("tenants", "users", "agent_runs", "knowledge_documents", "knowledge_document_versions",
                      "knowledge_chunks", "knowledge_index_jobs", "embedding_execution_budgets", "embedding_execution_attempts", "knowledge_vector_spaces"):
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
    c.execute("INSERT INTO knowledge_documents(id,tenant_id,user_id,title) VALUES(%s,%s,%s,'虚构政策')",
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


@pytest.fixture
def retrieval_database(database):
    """SQLAlchemy语句在同一可回滚PG事务执行；复用真实租户事务。"""
    from contextlib import asynccontextmanager
    from sqlalchemy.dialects.postgresql import dialect
    class Session:
        @asynccontextmanager
        async def begin(self):
            with database.transaction():
                yield
        async def execute(self, statement, params=None):
            cursor=database.execute(str(statement.compile(dialect=dialect(paramstyle='pyformat'))),params or {})
            rows=cursor.fetchall() if cursor.description else []
            return SimpleNamespace(rowcount=cursor.rowcount,mappings=lambda:SimpleNamespace(all=lambda:rows,one=lambda:rows[0],first=lambda:rows[0] if rows else None),
                first=lambda:tuple(rows[0].values()) if rows else None,
                scalar_one=lambda:next(iter(rows[0].values())))
    class Database:
        @asynccontextmanager
        async def session(self):
            yield Session()
    return Database()


def searchable(c, document=100, tenant=1, user=10, shared=False, content='续约需提前30天通知。合同CN-2026-01，联系ops@example.com，Aegis项目。'):
    from hashlib import sha256
    from aegis_agent_worker.indexing.chunker import search_tokens
    seed(c,document,tenant,user,shared)
    c.execute('UPDATE knowledge_documents SET enabled=true,external_embedding_allowed=true WHERE id=%s',(UUID(int=document),))
    c.execute('UPDATE knowledge_chunks SET content=%s,end_offset=%s,content_hash=%s,search_tokens=%s,indexed_at=now() WHERE document_id=%s',
        (content,len(content),sha256(content.encode()).hexdigest(),search_tokens(content),UUID(int=document)))
    namespace=sha256(b'https://test.example/kb').hexdigest()
    c.execute('UPDATE knowledge_document_versions SET vector_namespace=%s,vector_collection=%s WHERE id=%s',
        (namespace,'kb',UUID(int=document+1)))
    c.execute("""INSERT INTO knowledge_index_jobs(id,tenant_id,user_id,document_id,version_id,status,
        vector_namespace,vector_collection,total_chunks,processed_chunks)
        VALUES(%s,%s,%s,%s,%s,'succeeded',%s,'kb',1,1)""",
        (UUID(int=document+3),UUID(int=tenant),UUID(int=user),UUID(int=document),UUID(int=document+1),namespace))
    c.execute("""INSERT INTO knowledge_vector_spaces(namespace,collection_name,model_provider,model_name,model_version,dimensions)
        VALUES(%s,'kb','openai_compatible','test-model','test-v1',3) ON CONFLICT DO NOTHING""",(namespace,))


@pytest.mark.asyncio
async def test_actual_retrieval_scope_private_shared_cross_tenant_and_bypass(database,retrieval_database):
    from aegis_agent_worker.repository.knowledge_retrieval_repository import KnowledgeRetrievalRepository
    from aegis_agent_worker.tool.contracts import ToolContext
    searchable(database)
    searchable(database,document=200,shared=True)
    searchable(database,document=300,tenant=2,user=20,shared=True)
    repo=KnowledgeRetrievalRepository(retrieval_database)
    for tenant,user,expected in ((1,10,{101,201}),(1,11,{201}),(2,20,{301})):
        context=ToolContext(UUID(int=tenant),UUID(int=user),UUID(int=600))
        rows=await repo.scope(context,200)
        assert {row['version_id'].int for row in rows}==expected
    # 即使运行连接绕过RLS，显式SQL身份范围也不能省略。
    database.execute('RESET ROLE')
    rows=await repo.scope(ToolContext(UUID(int=1),UUID(int=11),UUID(int=600)),200)
    assert {row['version_id'].int for row in rows}=={201}
    context=ToolContext(UUID(int=1),UUID(int=11),UUID(int=600))
    assert not await repo.load(context,[UUID(int=101),UUID(int=301)],[UUID(int=102),UUID(int=302)])


@pytest.mark.asyncio
@pytest.mark.parametrize('query',['续约','ops@example.com','CN-2026-01','Aegis项目'])
async def test_actual_fts_chinese_email_identifier_and_mixed(database,retrieval_database,query):
    from aegis_agent_worker.repository.knowledge_retrieval_repository import KnowledgeRetrievalRepository
    from aegis_agent_worker.indexing.chunker import search_tokens
    from aegis_agent_worker.tool.contracts import ToolContext
    searchable(database)
    context=ToolContext(UUID(int=1),UUID(int=10),UUID(int=600))
    repo=KnowledgeRetrievalRepository(retrieval_database)
    tsquery=' | '.join("'"+term+"'" for term in search_tokens(query).split())
    hits=await repo.lexical(context,[UUID(int=101)],tsquery,20)
    assert [item.chunk_id.int for item in hits]==[102]
    assert (await repo.load(context,[UUID(int=101)],[UUID(int=102)]))[0].excerpt.startswith('续约')
    assert not await repo.lexical(context,[UUID(int=999)],tsquery,20)


@pytest.mark.asyncio
@pytest.mark.parametrize('change',["enabled=false","deleted_at=now()","is_sensitive=true","external_embedding_allowed=false",
    "current_version_id=NULL"])
async def test_actual_scope_and_delivery_state_guards(database,retrieval_database,change):
    from aegis_agent_worker.repository.knowledge_retrieval_repository import KnowledgeRetrievalRepository
    from aegis_agent_worker.tool.contracts import ToolContext
    searchable(database)
    context=ToolContext(UUID(int=1),UUID(int=10),UUID(int=600))
    repo=KnowledgeRetrievalRepository(retrieval_database)
    assert len(await repo.scope(context,200))==1
    database.execute('UPDATE knowledge_documents SET '+change)
    assert not await repo.scope(context,200)
    assert not await repo.lexical(context,[UUID(int=101)],"'续约'",20)
    assert not await repo.load(context,[UUID(int=101)],[UUID(int=102)])


@pytest.mark.asyncio
async def test_actual_hybrid_semantic_stub_embedding_audit_and_revocation(database,retrieval_database):
    from aegis_agent_worker.repository.knowledge_retrieval_repository import KnowledgeRetrievalRepository
    from aegis_agent_worker.service.knowledge.hybrid_retrieval_service import HybridRetrievalService
    from aegis_agent_worker.harness.embedding_executor import EmbeddingExecutor
    from aegis_agent_worker.embedding.contracts import EmbeddingProfile
    from aegis_agent_worker.common.usage import TokenUsage
    from aegis_agent_worker.retrieval.ranking import SearchHit
    from aegis_agent_worker.tool.contracts import ToolContext
    searchable(database,content='续约通知期限为30天。')
    settings=SimpleNamespace(embedding_provider='openai_compatible',embedding_model='test-model',embedding_model_version='test-v1',
        embedding_dimensions=3,qdrant_url='https://test.example',qdrant_collection='kb',knowledge_lexical_fallback_allowed=False)
    class Client:
        profile=EmbeddingProfile('openai_compatible','test-model','test-v1',3)
        max_batch_items=3
        usage=TokenUsage(8,None,8)
        def ensure_configured(self): pass
        def reset_usage(self): pass
        async def embed(self,texts): return [[1.,0.,0.] for _ in texts]
    class Vector:
        revoke=False
        async def search(self,context,versions,vector,profile,limit):
            assert versions==[UUID(int=101)]
            if self.revoke:
                database.execute('UPDATE knowledge_documents SET enabled=false')
            return [SearchHit(UUID(int=102),0.9),SearchHit(UUID(int=302),0.8)]
    vector=Vector()
    service=HybridRetrievalService(settings,KnowledgeRetrievalRepository(retrieval_database),
        EmbeddingExecutor(retrieval_database,Client(),external_queries_allowed=True),vector)
    context=ToolContext(UUID(int=1),UUID(int=10),UUID(int=600))
    # 同义问题不依赖词面重合；向量用替身，不将此结果声称为真实模型召回率。
    result=await service.search(context,{'query':'advance notice for renewal'})
    assert len(result.evidence)==1 and result.evidence[0].chunk_id.int==102
    assert result.retrieval.rankings[0].vector_rank==1 and result.retrieval.rankings[0].lexical_rank is None
    audit=database.execute("SELECT purpose,prompt_tokens FROM embedding_execution_attempts").fetchone()
    assert audit['purpose']=='knowledge_query' and audit['prompt_tokens']==8
    vector.revoke=True
    result=await service.search(context,{'query':'advance notice for renewal'})
    assert result.evidence==[] and result.retrieval.reason=='sources_unavailable'


@pytest.mark.asyncio
async def test_source_card_actual_sql_current_identity_and_revocation(database,retrieval_database):
    from app.service.knowledge_artifact_service import KnowledgeArtifactService
    from aegis_agent_worker.repository.knowledge_retrieval_repository import KnowledgeRetrievalRepository
    from aegis_agent_worker.tool.contracts import ToolContext
    searchable(database,shared=True)
    context=ToolContext(UUID(int=1),UUID(int=11),UUID(int=600))
    evidence=(await KnowledgeRetrievalRepository(retrieval_database).load(context,[UUID(int=101)],[UUID(int=102)]))[0]
    artifact={'artifact_type':'knowledge_sources','payload':{'sources':[evidence.model_dump(mode='json')]}}
    service=KnowledgeArtifactService()
    user=SimpleNamespace(id=context.user_id,tenant_id=context.tenant_id)
    async with retrieval_database.session() as session:
        assert (await service.refresh(session,user,artifact))['payload']['availability']=='available'
        identity(database)
        database.execute("UPDATE knowledge_documents SET visibility='private'")
        identity(database,user=11)
        result=await service.refresh(session,user,artifact)
        assert result['payload']['sources']==[] and result['payload']['availability']=='unavailable'
        # 即使连接绕过RLS，也不能读取其他租户或撤回共享后的旧来源。
        database.execute('RESET ROLE')
        result=await service.refresh(session,user,artifact)
        assert not result['payload']['sources']


@pytest.mark.asyncio
async def test_document_management_source_hash_and_soft_delete_actual_sql(database,retrieval_database):
    from app.repository.knowledge_document_repository import KnowledgeDocumentRepository
    from app.repository.knowledge_import_repository import KnowledgeImportRepository
    from app.repository.knowledge_repository import KnowledgeRepository
    searchable(database,shared=True)
    owner=SimpleNamespace(id=UUID(int=10),tenant_id=UUID(int=1))
    reader=SimpleNamespace(id=UUID(int=11),tenant_id=UUID(int=1))
    repo=KnowledgeDocumentRepository()
    digest=database.execute('SELECT content_hash FROM knowledge_chunks').fetchone()['content_hash']
    async with retrieval_database.session() as session:
        identity(database,user=11)
        detail=await repo.detail(session,reader,UUID(int=100))
        assert detail['index_status']=='ready' and not detail['can_manage'] and detail['job_id'] is None
        assert (await KnowledgeImportRepository().list_documents(session,reader,10))[0]['index_status']=='ready'
        assert not await repo.delete(session,reader,UUID(int=100))
        assert await repo.chunk(session,reader,UUID(int=100),UUID(int=101),UUID(int=102),digest)
        assert not await repo.chunk(session,reader,UUID(int=100),UUID(int=101),UUID(int=102),'c'*64)
        assert not await repo.chunk(session,reader,UUID(int=999),UUID(int=101),UUID(int=102),digest)
        identity(database)
        assert await KnowledgeRepository().set_visibility(session,tenant_id=owner.tenant_id,user_id=owner.id,document_id=UUID(int=100),visibility='private')
        identity(database,user=11)
        assert not await repo.chunk(session,reader,UUID(int=100),UUID(int=101),UUID(int=102),digest)
        identity(database)
        assert await repo.delete(session,owner,UUID(int=100))
        assert not await repo.detail(session,owner,UUID(int=100))
        assert not await repo.chunk(session,owner,UUID(int=100),UUID(int=101),UUID(int=102),digest)
        assert database.execute('SELECT count(*) AS n FROM knowledge_chunks').fetchone()['n']==0
        assert database.execute('SELECT count(*) AS n FROM knowledge_document_versions').fetchone()['n']==0
        assert database.execute('SELECT count(*) AS n FROM knowledge_index_jobs').fetchone()['n']==0
        database.execute('RESET ROLE')
        assert database.execute('SELECT count(*) AS n FROM knowledge_chunks').fetchone()['n']==1
        assert not await repo.chunk(session,owner,UUID(int=100),UUID(int=101),UUID(int=102),digest)


@pytest.mark.asyncio
async def test_shared_reader_hybrid_binding_does_not_require_private_job_access(database,retrieval_database):
    from aegis_agent_worker.repository.knowledge_retrieval_repository import KnowledgeRetrievalRepository
    from aegis_agent_worker.service.knowledge.hybrid_retrieval_service import HybridRetrievalService
    from aegis_agent_worker.tool.contracts import ToolContext
    from aegis_agent_worker.retrieval.ranking import SearchHit
    searchable(database,shared=True)
    identity(database,user=11)
    assert not database.execute('SELECT id FROM knowledge_index_jobs').fetchall()
    settings=SimpleNamespace(embedding_provider='openai_compatible',embedding_model='test-model',embedding_model_version='test-v1',
        embedding_dimensions=3,qdrant_url='https://test.example',qdrant_collection='kb',knowledge_lexical_fallback_allowed=False)
    class Embedding:
        async def embed(self,subject,texts): return [[1.,0.,0.] for _ in texts]
    class Vector:
        async def search(self,context,versions,vector,profile,limit):
            assert versions==[UUID(int=101)]
            return [SearchHit(UUID(int=102),1.)]
    result=await HybridRetrievalService(settings,KnowledgeRetrievalRepository(retrieval_database),Embedding(),Vector()).search(
        ToolContext(UUID(int=1),UUID(int=11),UUID(int=601)),{'query':'续约'})
    assert len(result.evidence)==1 and result.evidence[0].chunk_id.int==102


def test_version_binding_migration_backfills_and_roundtrips(database):
    searchable(database,shared=True)
    database.execute('SET CONSTRAINTS ALL IMMEDIATE')
    database.execute('RESET ROLE')
    apply(database,'015','downgrade')
    apply(database,'015')
    row=database.execute('SELECT vector_namespace,vector_collection FROM knowledge_document_versions').fetchone()
    assert row['vector_collection']=='kb' and len(row['vector_namespace'])==64


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
    apply(database, "015", "downgrade")
    apply(database, "013", "downgrade")
    apply(database, "012", "downgrade")
    apply(database, "011", "downgrade")
    apply(database, "010", "downgrade")
    apply(database, "010")
    apply(database, "011")
    apply(database, "012")
    apply(database, "013")
    apply(database, "015")
    assert database.execute("SELECT count(*) AS n FROM knowledge_documents").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_real_sql_import_index_lease_and_idempotent_resume(database,tmp_path):
    from contextlib import asynccontextmanager
    from hashlib import sha256
    from sqlalchemy.dialects.postgresql import dialect
    from app.service.knowledge_import_service import KnowledgeImportService
    from app.param.knowledge_param import KnowledgeImportMetadata,KnowledgeIndexStartParam
    from app.storage.local_document_storage import LocalDocumentStorage
    from aegis_agent_worker.repository.knowledge_index_repository import KnowledgeIndexRepository
    from aegis_agent_worker.service.knowledge.knowledge_index_service import KnowledgeIndexService
    from aegis_agent_worker.harness.embedding_executor import EmbeddingExecutor
    from aegis_agent_worker.embedding.contracts import EmbeddingProfile
    from aegis_agent_worker.common.usage import TokenUsage
    class Result:
        def __init__(self,rows): self.rows=rows
        def first(self): return tuple(self.rows[0].values()) if self.rows else None
        def mappings(self): return SimpleNamespace(first=lambda:self.rows[0] if self.rows else None,
            all=lambda:self.rows,one=lambda:self.rows[0])
        def scalar_one(self): return next(iter(self.rows[0].values()))
    class Session:
        @asynccontextmanager
        async def begin(self):
            with database.transaction(): yield
        async def execute(self,statement,params=None):
            compiled=statement.compile(dialect=dialect(paramstyle='pyformat'))
            cursor=database.execute(str(compiled),params or {})
            return Result(cursor.fetchall() if cursor.description else [])
    class Database:
        @asynccontextmanager
        async def session(self): yield Session()
    user=SimpleNamespace(id=UUID(int=10),tenant_id=UUID(int=1))
    identity(database)
    settings=SimpleNamespace(knowledge_max_source_bytes=262144,embedding_provider='openai_compatible',embedding_model='test-model',
        embedding_model_version='test-v1',embedding_dimensions=3,embedding_max_batch_items=10,qdrant_collection='test',
        knowledge_index_lease_seconds=180,knowledge_index_max_claims=3)
    imports=KnowledgeImportService(settings,LocalDocumentStorage(tmp_path))
    param=KnowledgeImportMetadata(title='虚构政策',client_request_id='db-e2e',external_embedding_allowed=True)
    item=await imports.import_source(Session(),user,param,'项目政策。'.encode(),'policy.txt')
    replay=await imports.import_source(Session(),user,param,'项目政策。'.encode(),'policy.txt')
    assert item['document_id']==replay['document_id'] and replay['idempotent_replay']
    assert item['job_id'] is None and item['index_status']=='not_indexed' and not item['enabled']
    assert database.execute('SELECT count(*) AS n FROM knowledge_index_jobs').fetchone()['n']==0
    with pytest.raises(Exception,match='索引完成'):
        await imports.activate(Session(),user,item['document_id'],True)
    item=await imports.start_index(Session(),user,item['document_id'],KnowledgeIndexStartParam(external_embedding_allowed=True))
    restarted=await imports.start_index(Session(),user,item['document_id'],KnowledgeIndexStartParam(external_embedding_allowed=True))
    assert restarted['job_id']==item['job_id'] and restarted['idempotent_replay']
    message=dict(job_id=item['job_id'],tenant_id=user.tenant_id,user_id=user.id)
    repo=KnowledgeIndexRepository(Database())
    assert await repo.recoverable(30)==[message]
    token=uuid4()
    assert await repo.claim(message,token,180,3)
    assert await repo.claim(message,uuid4(),180,3) is None
    # 模拟消息已取走后崩溃；SQL过期租约允许恢复，预算仍使用同一job。
    database.execute('UPDATE knowledge_index_jobs SET lease_expires_at=now()-interval \'1 second\' WHERE id=%s',(item['job_id'],))
    class Client:
        profile=EmbeddingProfile('openai_compatible','test-model','test-v1',3)
        max_batch_items=10
        usage=TokenUsage(5,None,5)
        def ensure_configured(self): pass
        def reset_usage(self): pass
        async def embed(self,texts): return [[1.,2.,3.] for _ in texts]
    class Index:
        namespace='a'*64
        async def ensure_collection(self,profile): return {'points_count':0}
        async def upsert(self,points):
            assert 'content' not in points[0]['payload']
    service=KnowledgeIndexService(settings,repo,LocalDocumentStorage(tmp_path),EmbeddingExecutor(Database(),Client()),Index())
    result=await service.execute(message)
    assert result['status']=='succeeded'
    assert (await service.execute(message))['status']=='ignored'
    stored=database.execute('SELECT status,processed_chunks,total_chunks,attempt_count FROM knowledge_index_jobs WHERE id=%s',(item['job_id'],)).fetchone()
    assert stored['status']=='succeeded' and stored['processed_chunks']==stored['total_chunks']==1 and stored['attempt_count']==2
    assert database.execute('SELECT current_version_id FROM knowledge_documents WHERE id=%s',(item['document_id'],)).fetchone()['current_version_id']==item['version_id']
    assert not database.execute('SELECT enabled FROM knowledge_documents WHERE id=%s',(item['document_id'],)).fetchone()['enabled']
    # 索引完成不等于可检索：禁用时丢弃证据，启用后才能复核，禁用立即停止新检索。
    from aegis_agent_worker.agent.contracts.knowledge import RetrievedEvidence
    from aegis_agent_worker.tool.contracts import ToolContext
    from aegis_agent_worker.repository.knowledge_evidence_repository import KnowledgeEvidenceRepository
    row=database.execute('SELECT * FROM knowledge_chunks WHERE version_id=%s',(item['version_id'],)).fetchone()
    evidence=RetrievedEvidence(source_no=1,document_id=item['document_id'],version_id=item['version_id'],chunk_id=row['id'],
        title='虚构政策',version_label='v1',section=row['section'] or '正文',start_offset=row['start_offset'],end_offset=row['end_offset'],
        excerpt=row['content'],content_hash=row['content_hash'])
    evidence_repo=KnowledgeEvidenceRepository(Database())
    context=ToolContext(user.tenant_id,user.id,UUID(int=700))
    assert await evidence_repo.revalidate(context,[evidence])==[]
    assert (await imports.activate(Session(),user,item['document_id'],True))['enabled']
    assert await evidence_repo.revalidate(context,[evidence])==[evidence]
    assert not (await imports.activate(Session(),user,item['document_id'],False))['enabled']
    assert await evidence_repo.revalidate(context,[evidence])==[]
    assert database.execute('SELECT call_count FROM embedding_execution_budgets WHERE index_job_id=%s',(item['job_id'],)).fetchone()['call_count']==1
    # 外置源文件降级必须拒绝，不能静默删除资料。
    database.execute('RESET ROLE')
    with pytest.raises(Exception,match='KNOWLEDGE_EXTERNAL_SOURCE_EXISTS'),database.transaction():
        apply(database,'012','downgrade')


def test_lifecycle_migration_backfills_requests_and_defaults_old_documents_disabled(database):
    database.execute('RESET ROLE')
    apply(database,'013','downgrade')
    seed(database)
    database.execute("INSERT INTO knowledge_index_jobs(id,tenant_id,user_id,document_id,version_id,client_request_id,request_hash) VALUES(%s,%s,%s,%s,%s,'legacy-upload',%s)",
        (UUID(int=500),UUID(int=1),UUID(int=10),UUID(int=100),UUID(int=101),'c'*64))
    # 测试全部在一个外层事务中；真实升级前旧数据已提交，这里先结算延迟外键事件模拟该边界。
    database.execute('SET CONSTRAINTS ALL IMMEDIATE')
    apply(database,'013')
    row=database.execute('SELECT enabled,client_request_id,request_hash FROM knowledge_documents WHERE id=%s',(UUID(int=100),)).fetchone()
    assert not row['enabled'] and row['client_request_id']=='legacy-upload' and row['request_hash']=='c'*64


def test_lifecycle_downgrade_rejects_unbound_uploaded_sources(database):
    database.execute('RESET ROLE')
    seed(database)
    database.execute('DELETE FROM knowledge_chunks')
    database.execute('UPDATE knowledge_document_versions SET embedding_provider=NULL,embedding_model=NULL,embedding_model_version=NULL,embedding_dimensions=NULL')
    with pytest.raises(Exception,match='KNOWLEDGE_UNINDEXED_SOURCE_EXISTS'),database.transaction():
        apply(database,'013','downgrade')


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
