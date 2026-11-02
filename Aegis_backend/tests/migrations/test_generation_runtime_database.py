"""025 actual SQL + governed fake vector build + query/context/history/publication.
All tables/roles in one rolled-back local schema; no paid or remote services.
"""
import importlib.util
import os
from pathlib import Path
from contextlib import asynccontextmanager
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4,UUID
import pytest
from test_shadow_generation_database import database

pytestmark=pytest.mark.skipif(os.getenv('AEGIS_GENERATION_DB_TESTS')!='1',reason='Opt-in local rollback-only integration')


@pytest.fixture
def runtime_database(database):
    conn,session,user,doc,ver,raw,identity=database
    from psycopg import sql
    role=conn.execute('SHOW role').fetchone()['role'];conn.execute('RESET ROLE')
    conn.execute("ALTER TABLE knowledge_documents ADD COLUMN enabled boolean DEFAULT true,ADD COLUMN visibility text DEFAULT 'private',ADD COLUMN external_rerank_allowed boolean DEFAULT false")
    namespace=sha256(b'https://vector.example/kb').hexdigest()
    conn.execute('''ALTER TABLE knowledge_document_versions ADD COLUMN version_label text DEFAULT 'v1',
      ADD COLUMN embedding_provider text DEFAULT 'openai_compatible',ADD COLUMN embedding_model text DEFAULT 'fake',
      ADD COLUMN embedding_model_version text DEFAULT 'v1',ADD COLUMN embedding_dimensions integer DEFAULT 3,
      ADD COLUMN vector_namespace text,ADD COLUMN vector_collection text DEFAULT 'kb' ''')
    conn.execute('UPDATE knowledge_document_versions SET vector_namespace=%s',(namespace,))
    conn.execute('''CREATE TABLE knowledge_chunks(id uuid PRIMARY KEY,document_id uuid,version_id uuid,tenant_id uuid,
      position integer,section text,content text,start_offset integer,end_offset integer,content_hash text,
      indexed_at timestamptz,search_text tsvector)''')
    legacy=uuid4()
    conn.execute("INSERT INTO knowledge_chunks VALUES(%s,%s,%s,%s,0,'正文','旧片段',0,3,%s,now(),to_tsvector('simple','旧片段'))",(legacy,doc,ver,user.tenant_id,sha256('旧片段'.encode()).hexdigest()))
    conn.execute('''CREATE TABLE embedding_execution_budgets(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id uuid,user_id uuid,
      run_id uuid UNIQUE,index_job_id uuid UNIQUE,policy_version text,model_provider text,model_name text,model_version text,dimensions integer,
      call_count integer DEFAULT 0,input_chars bigint DEFAULT 0,CONSTRAINT embedding_execution_budgets_check CHECK((run_id IS NULL)<>(index_job_id IS NULL)))''')
    conn.execute('''CREATE TABLE embedding_execution_attempts(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),budget_id uuid,tenant_id uuid,user_id uuid,
      purpose text,input_chars bigint,batch_items integer,status text DEFAULT 'running',error_code text,duration_ms bigint,
      prompt_tokens bigint,total_tokens bigint,finished_at timestamptz)''')
    path=Path(__file__).resolve().parents[2]/'migrations/versions/025_generation_runtime.py'
    spec=importlib.util.spec_from_file_location('generation_runtime_migration',path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    with patch.object(mod,'op',SimpleNamespace(execute=conn.execute)):mod.upgrade()
    conn.execute('''CREATE TABLE knowledge_index_jobs(id uuid PRIMARY KEY,tenant_id uuid,user_id uuid,document_id uuid,
      version_id uuid,status text DEFAULT 'queued',attempt_count integer DEFAULT 0,error_code text,
      lease_token uuid,lease_expires_at timestamptz,last_dispatched_at timestamptz,
      total_chunks integer DEFAULT 0,processed_chunks integer DEFAULT 0,vector_namespace text,vector_collection text,
      parser_version text,created_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now())''')
    conn.execute('ALTER TABLE knowledge_documents ADD COLUMN updated_at timestamptz DEFAULT now()')
    conn.execute('''ALTER TABLE knowledge_document_versions ADD COLUMN version_no integer DEFAULT 1,
      ADD COLUMN source_text text,ADD COLUMN storage_backend text,ADD COLUMN storage_key text,
      ADD COLUMN original_filename text,ADD COLUMN source_bytes integer''')
    conn.execute('''CREATE TABLE knowledge_vector_spaces(namespace text PRIMARY KEY,collection_name text,
      model_provider text,model_name text,model_version text,dimensions integer)''')
    path=path.with_name('026_automatic_indexing.py')
    spec=importlib.util.spec_from_file_location('automatic_generation_migration',path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    with patch.object(mod,'op',SimpleNamespace(execute=conn.execute)):mod.upgrade()
    # Test doc RLS as in production, not a Python-only authorization filter.
    conn.execute('ALTER TABLE knowledge_documents ENABLE ROW LEVEL SECURITY')
    conn.execute('ALTER TABLE knowledge_documents FORCE ROW LEVEL SECURITY')
    conn.execute("CREATE POLICY doc_read ON knowledge_documents FOR SELECT USING(tenant_id=aegis_current_tenant_id() AND (user_id=aegis_current_user_id() OR visibility='tenant') AND deleted_at IS NULL)")
    conn.execute("CREATE POLICY doc_write ON knowledge_documents FOR UPDATE USING(tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id())")
    schema=conn.execute('SELECT current_schema() AS s').fetchone()['s']
    conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(role)))
    conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
    @asynccontextmanager
    async def begin():yield session
    session.begin=begin
    class DB:
        @asynccontextmanager
        async def session(self):yield session
    db=DB()
    return (*database,db,namespace)


@pytest.mark.asyncio
async def test_generation_build_publish_query_history_revoke_and_rollback(runtime_database):
    from psycopg import sql
    from aegis_agent_worker.service.knowledge.shadow_generation_service import ShadowGenerationService
    from aegis_agent_worker.service.knowledge.generation_release_service import GenerationReleaseService
    from aegis_agent_worker.service.knowledge.generation_index_service import GenerationIndexService
    from aegis_agent_worker.repository.generation_build_repository import GenerationBuildRepository
    from aegis_agent_worker.harness.embedding_executor import EmbeddingExecutor
    from aegis_agent_worker.embedding.contracts import EmbeddingProfile
    from aegis_agent_worker.common.usage import TokenUsage
    from aegis_agent_worker.harness.context import bind_execution,reset_execution
    from aegis_agent_worker.harness.policy_provider import with_generations,LEGACY_POLICY
    from aegis_agent_worker.repository.generation_retrieval_repository import GenerationRetrievalRepository
    from aegis_agent_worker.repository.knowledge_evidence_repository import KnowledgeEvidenceRepository
    from aegis_agent_worker.service.knowledge.generation_context_service import GenerationContextService
    from aegis_agent_worker.service.knowledge.hybrid_retrieval_service import HybridRetrievalService
    from aegis_agent_worker.agent.contracts.knowledge import KnowledgeQueryPlan,KnowledgeSourceArtifact,KnowledgeSourcesPayload
    from aegis_agent_worker.retrieval.ranking import SearchHit
    from aegis_agent_worker.tool.contracts import ToolContext
    from app.service.knowledge_artifact_service import KnowledgeArtifactService
    conn,session,user,doc,ver,raw,identity,db,namespace=runtime_database
    legacy=conn.execute('SELECT id FROM knowledge_chunks LIMIT 1').fetchone()['id']
    settings=SimpleNamespace(embedding_provider='openai_compatible',embedding_model='fake',embedding_model_version='v1',embedding_dimensions=3,
        qdrant_url='https://vector.example',qdrant_collection='kb',knowledge_lexical_fallback_allowed=False)
    profile=EmbeddingProfile('openai_compatible','fake','v1',3)
    class Client:
        max_batch_items=10;usage=TokenUsage(total_tokens=1)
        def ensure_configured(self):pass
        def reset_usage(self):pass
        @property
        def profile(self):return profile
        async def embed(self,texts):return [[1.,0.,0.] for _ in texts]
    class Index:
        def __init__(self):self.namespace=namespace;self.points=[]
        async def ensure_collection(self,p):return {}
        async def upsert(self,points):self.points.extend(points)
    index=Index();embedding=EmbeddingExecutor(db,Client())
    shadow=ShadowGenerationService();release=GenerationReleaseService()
    a=await shadow.prepare(session,user,doc,ver,'runtime-1',raw,dict(size=12,overlap=0))
    await shadow.validate(session,user,doc,ver,a['generation_id'],a['manifest_hash'],'structural-unit-test')
    await release.enqueue(session,user,doc,ver,a['generation_id'])
    from aegis_agent_worker.harness.exceptions import HarnessError
    with pytest.raises(HarnessError):await release.publish(session,user,doc,ver,a['generation_id'],0,'not-ready')
    item=dict(generation_id=a['generation_id'],tenant_id=user.tenant_id,user_id=user.id)
    build=GenerationIndexService(settings,GenerationBuildRepository(db),embedding,index)
    assert (await build.execute(item))['status']=='ready'
    assert index.points and conn.execute('SELECT count(*) AS n FROM embedding_execution_attempts').fetchone()['n']>0
    assert (await build.execute(item))['status']=='ignored'
    await release.publish(session,user,doc,ver,a['generation_id'],0,'test-only-approval')
    with pytest.raises(HarnessError):await release.publish(session,user,doc,ver,a['generation_id'],0,'stale-revision')
    active=conn.execute('SELECT id,generation_id FROM knowledge_runtime_chunks').fetchall()
    assert active and all(r['generation_id']==a['generation_id'] for r in active) and all(r['id']!=legacy for r in active)
    token=bind_execution(SimpleNamespace(id=uuid4()),with_generations(LEGACY_POLICY))
    context=ToolContext(user.tenant_id,user.id,uuid4())
    repo=GenerationRetrievalRepository(db)
    # Same SQL, infrastructure-model map precheck is not the focus of this fixture.
    async def check_space(*args):return True
    repo.check_space=check_space
    class Vector:
        async def search(self,ctx,versions,vector,p,limit):
            ids=repo.filter_ids(ctx)
            assert legacy not in ids
            return [SearchHit(cid,1/(i+1)) for i,cid in enumerate(ids[:limit])]
    class QueryEmbedding:
        async def embed(self,*args):return [[1.,0.,0.]]
    try:
        service=HybridRetrievalService(settings,repo,QueryEmbedding(),Vector(),context_expansion=GenerationContextService(repo))
        result=await service.search(context,KnowledgeQueryPlan(query='条件',top_k=1))
        assert result.evidence and result.evidence[0].generation_id==a['generation_id']
        assert result.evidence[0].parts
        verified=await KnowledgeEvidenceRepository(db).revalidate(context,result.evidence)
        assert verified==result.evidence
        artifact=KnowledgeSourceArtifact(payload=KnowledgeSourcesPayload(sources=verified)).to_artifact().model_dump(mode='json')
        assert artifact['artifact_type']=='knowledge_sources_v2'
        refreshed=await KnowledgeArtifactService().refresh(session,user,artifact)
        assert refreshed['payload']['availability']=='available'
        conn.execute("UPDATE knowledge_document_versions SET content_hash=%s",('0'*64,))
        assert (await KnowledgeArtifactService().refresh(session,user,artifact))['payload']['availability']=='unavailable'
        conn.execute('UPDATE knowledge_document_versions SET content_hash=%s',(sha256(raw).hexdigest(),))
        # Exercise the real knowledge subgraph through the actual internal tool
        # gateway, with a deterministic answer adapter (not a real LLM score).
        from aegis_agent_worker.agent.workflows.knowledge.query_workflow import KnowledgeQueryWorkflow
        from aegis_agent_worker.service.knowledge.evidence_service import EvidenceService
        from aegis_agent_worker.agent.contracts.knowledge import KnowledgeAnswer,KnowledgeSearchResult
        from aegis_agent_worker.tool.registry import ToolRegistry
        from aegis_agent_worker.tool.gateway import ToolGateway
        from aegis_agent_worker.tool.contracts import ToolDefinition
        from aegis_agent_worker.tool.knowledge.handler import KnowledgeSearchHandler
        registry=ToolRegistry()
        registry.register(ToolDefinition('knowledge.search','1','read','internal','查询',argument_schema=KnowledgeQueryPlan),KnowledgeSearchHandler(service))
        gateway=ToolGateway(registry)
        class Query:
            async def query(self,run,plan):
                call=await gateway.prepare(None,tenant_id=run.tenant_id,user_id=run.user_id,run_id=run.id,invocation=plan.to_invocation())
                answer=await gateway.invoke(call)
                return KnowledgeSearchResult.model_validate(answer.response_payload)
        class Agent:
            async def plan_query(self,*args):return KnowledgeQueryPlan(query='条件',top_k=1)
            async def generate_answer(self,query,evidence):
                assert evidence[0].parts
                return KnowledgeAnswer(answer='依据当前原文及附加范围核对。[1]',citation_numbers=[1],can_confirm=True)
        class Artifacts:
            saved=[]
            async def persist(self,run,items):self.saved.extend(items)
        artifacts=Artifacts()
        graph=KnowledgeQueryWorkflow(Agent(),Query(),EvidenceService(KnowledgeEvidenceRepository(db)),artifacts).build()
        state=dict(run_id=str(context.run_id),tenant_id=str(user.tenant_id),user_id=str(user.id),conversation_id=str(uuid4()),
            run_type='conversation',latest_user_message='条件',handoff=dict(domain='knowledge',intent='query_knowledge',instruction='查询条件'))
        output=await graph.ainvoke(state)
        assert output['knowledge_status']=='succeeded' and artifacts.saved[0].artifact_type=='knowledge_sources_v2'
        await release.publish(session,user,doc,ver,None,1,'rollback-test',rollback=True)
        assert conn.execute('SELECT id FROM knowledge_runtime_chunks').fetchone()['id']==legacy
        assert (await KnowledgeArtifactService().refresh(session,user,artifact))['payload']['availability']=='available'
        other=SimpleNamespace(id=uuid4(),tenant_id=user.tenant_id)
        identity(other)
        assert (await KnowledgeArtifactService().refresh(session,other,artifact))['payload']['availability']=='unavailable'
        identity(user)
        conn.execute("UPDATE knowledge_documents SET visibility='tenant'")
        identity(other)
        assert (await KnowledgeArtifactService().refresh(session,other,artifact))['payload']['availability']=='available'
        identity(user)
        conn.execute('UPDATE knowledge_documents SET enabled=false')
        assert not await KnowledgeEvidenceRepository(db).revalidate(context,result.evidence)
        assert (await KnowledgeArtifactService().refresh(session,user,artifact))['payload']['availability']=='unavailable'
    finally:reset_execution(token)
