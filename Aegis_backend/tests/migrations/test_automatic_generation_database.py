"""Start-index → governed structure build → explicit publish/enable, rollback-only SQL."""
from types import SimpleNamespace
from uuid import uuid5
import pytest
from test_generation_runtime_database import database,runtime_database,pytestmark


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupt', [None,'upsert','parent_complete','crash'])
async def test_automatic_index_retry_publication_and_activation(runtime_database,interrupt):
    from app.service.knowledge_import_service import KnowledgeImportService,KnowledgeImportError
    from app.service.generation_publication_service import GenerationPublicationService
    from app.param.generation_publication_param import GenerationPublicationParam
    from aegis_agent_worker.repository.knowledge_index_repository import KnowledgeIndexRepository
    from aegis_agent_worker.repository.generation_build_repository import GenerationBuildRepository
    from aegis_agent_worker.service.knowledge.knowledge_index_service import KnowledgeIndexService
    from aegis_agent_worker.service.knowledge.generation_index_service import GenerationIndexService
    from aegis_agent_worker.harness.embedding_executor import EmbeddingExecutor
    from aegis_agent_worker.embedding.contracts import EmbeddingProfile
    from aegis_agent_worker.common.usage import TokenUsage
    from aegis_agent_worker.harness.exceptions import HarnessError
    conn,session,user,doc,ver,raw,identity,db,namespace=runtime_database
    conn.execute('UPDATE knowledge_documents SET current_version_id=NULL,enabled=false')
    conn.execute("UPDATE knowledge_document_versions SET status='pending',source_text=%s,source_bytes=%s",(raw.decode(),len(raw)))
    settings=SimpleNamespace(knowledge_index_strategy='structure-v1',knowledge_index_title_enhancement=True,
        embedding_provider='openai_compatible',embedding_model='fake',embedding_model_version='v1',embedding_dimensions=3,
        qdrant_collection='kb',knowledge_index_lease_seconds=180,knowledge_index_max_claims=3,knowledge_max_source_bytes=100000)
    imports=KnowledgeImportService(settings,None)
    item=await imports.start_index(session,user,doc,SimpleNamespace(external_embedding_allowed=True))
    job_id=item['job_id'];gid=uuid5(job_id,'automatic-generation-v1')
    message=dict(job_id=job_id,tenant_id=user.tenant_id,user_id=user.id)
    assert (await imports.start_index(session,user,doc,SimpleNamespace(external_embedding_allowed=True)))['idempotent_replay']
    class Client:
        max_batch_items=10;usage=TokenUsage(total_tokens=2)
        profile=EmbeddingProfile('openai_compatible','fake','v1',3)
        def ensure_configured(self):pass
        def reset_usage(self):pass
        async def embed(self,texts):return [[1.,0.,0.] for _ in texts]
    class Vector:
        def __init__(self):self.namespace=namespace;self.points={};self.fail=interrupt=='upsert'
        async def ensure_collection(self,profile):return dict(points_count=0)
        async def upsert(self,points):
            if self.fail:
                self.fail=False
                raise HarnessError('QDRANT_UNAVAILABLE','test failure')
            self.points.update({p['id']:p for p in points})
            if interrupt=='crash' and not getattr(self,'crashed',False):
                import asyncio
                self.crashed=True
                raise asyncio.CancelledError()
    class Repository(KnowledgeIndexRepository):
        fail_complete=interrupt=='parent_complete'
        async def complete(self,*args):
            if self.fail_complete:
                self.fail_complete=False
                raise HarnessError('KNOWLEDGE_LEASE_LOST','test crash after ready build')
            return await super().complete(*args)
    embedding=EmbeddingExecutor(db,Client());vector=Vector();repo=Repository(db)
    builder=GenerationIndexService(settings,GenerationBuildRepository(db),embedding,vector)
    worker=KnowledgeIndexService(settings,repo,None,embedding,vector,generation_builder=builder)
    before=conn.execute('SELECT count(*) AS n FROM knowledge_chunks').fetchone()['n']
    if interrupt=='crash':
        import asyncio
        with pytest.raises(asyncio.CancelledError):await worker.execute(message)
        # Parent expires first; a fresh claimant fences the lingering child lease.
        conn.execute("UPDATE knowledge_index_jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s",(job_id,))
        result={'status':'failed'}
    else:result=await worker.execute(message)
    if interrupt:
        assert result['status']=='failed'
        budget=conn.execute('SELECT id,call_count FROM embedding_execution_budgets WHERE generation_id=%s',(gid,)).fetchone()
        assert budget['call_count']==1
        if interrupt!='crash':assert (await imports.retry(session,user,job_id))['status']=='queued'
        result=await worker.execute(message)
        after=conn.execute('SELECT id,call_count FROM embedding_execution_budgets WHERE generation_id=%s',(gid,)).fetchone()
        assert after['id']==budget['id'] and after['call_count']==(1 if interrupt=='parent_complete' else 2)
    assert result['status']=='succeeded' and result['generation_id']==str(gid) and result['published'] is False
    assert (await worker.execute(message))['status']=='ignored'
    assert conn.execute('SELECT count(*) AS n FROM knowledge_chunks').fetchone()['n']==before
    job=await imports.job(session,user,job_id)
    assert job['index_strategy']=='structure-v1' and job['generation_id']==gid
    assert job['processed_chunks']==job['total_chunks']>0 and 'index_config' not in job
    assert conn.execute('SELECT count(*) AS n FROM knowledge_generation_publications').fetchone()['n']==0
    assert not conn.execute('SELECT enabled FROM knowledge_documents').fetchone()['enabled']
    # Changing the default for future jobs cannot bypass an existing job's publication gate.
    settings.knowledge_index_strategy='paragraph-v1'
    with pytest.raises(KnowledgeImportError,match='尚未明确发布'):
        await imports.activate(session,user,doc,True)
    svc=GenerationPublicationService()
    status=await svc.status(session,user,doc)
    assert status['revision']==0 and status['generation_status']=='ready'
    param=GenerationPublicationParam(version_id=ver,generation_id=gid,expected_revision=0,review_reference='test review')
    published=await svc.publish(session,user,doc,param)
    assert published['revision']==1 and published['changed']
    # Retry CAS is deterministic; stale revisions fail without altering the head.
    import psycopg
    with pytest.raises(psycopg.Error,match='GENERATION_REVISION_CONFLICT'):
        with conn.transaction():await svc.repo.publish(session,user,doc,param)
    assert (await imports.activate(session,user,doc,True))['enabled']
    assert conn.execute('SELECT count(*) AS n FROM knowledge_runtime_chunks WHERE generation_id=%s',(gid,)).fetchone()['n']>0
    # RLS/current identity is the authority, including direct SQL function calls.
    other=SimpleNamespace(id=user.id,tenant_id=__import__('uuid').uuid4())
    identity(other)
    assert await svc.repo.status(session,user,doc) is None
    with pytest.raises(psycopg.Error,match='GENERATION_PUBLICATION_REJECTED'):
        with conn.transaction():await svc.repo.publish(session,user,doc,param)
    identity(user)
