"""6.3离线测试：源文件/切块、受控远端写入和可恢复流程。"""
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import httpx
import pytest
from pydantic import SecretStr
from aegis_agent_worker.indexing.chunker import chunk_text
from aegis_agent_worker.storage.local_document_storage import LocalDocumentStorage
from aegis_agent_worker.service.knowledge.knowledge_index_service import KnowledgeIndexService
from aegis_agent_worker.embedding.contracts import EmbeddingProfile
from aegis_agent_worker.vector.qdrant_index import QdrantVectorIndex
from aegis_agent_worker.vector.contracts import VectorIndexError
from aegis_agent_worker.worker.index_worker import parse_message


def test_chunk_positions_hash_and_deterministic_ids():
    source='# 标题\n\n'+'项目续约规则。'*300
    chunks=chunk_text(source,UUID(int=1))
    assert len(chunks)>1 and chunks==chunk_text(source,UUID(int=1))
    for chunk in chunks:
        assert source[chunk.start_offset:chunk.end_offset]==chunk.content
        assert chunk.content_hash==sha256(chunk.content.encode()).hexdigest()
        assert len(chunk.content)<=700 and '续约' in chunk.search_tokens


@pytest.mark.asyncio
async def test_local_storage_is_bounded_and_immutable(tmp_path):
    store=LocalDocumentStorage(tmp_path,10)
    key=str(UUID(int=1))
    await store.put(key,b'text')
    assert await store.read(key)==b'text'
    with pytest.raises(ValueError): await store.put(key,b'new')
    assert await store.read(key)==b'text'
    with pytest.raises(ValueError): await store.read('../outside')
    with pytest.raises(ValueError): await store.put(str(UUID(int=2)),b'a'*11)


def vector_settings():
    return SimpleNamespace(qdrant_url='https://vector.invalid',qdrant_collection='test',qdrant_enabled=True,
        qdrant_write_api_key=SecretStr('test'),qdrant_request_timeout_seconds=2)


@pytest.mark.asyncio
async def test_vector_create_and_write_require_confirmation_without_text_payload():
    calls=[]
    def handler(request):
        calls.append((request.method,request.url.path,request.url.query))
        if request.method=='GET' and len(calls)==1:
            return httpx.Response(404)
        if request.method=='GET':
            return httpx.Response(200,json={'status':'ok','result':{'config':{'params':{'vectors':{'size':3,'distance':'Cosine'}}},'points_count':0}})
        if request.url.path.endswith('/points'):
            assert request.url.query==b'wait=true'
            return httpx.Response(200,json={'status':'ok','result':{'status':'completed'}})
        return httpx.Response(200,json={'status':'ok','result':True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        index=QdrantVectorIndex(vector_settings(),client)
        await index.ensure_collection(EmbeddingProfile('compatible','model','v1',3))
        await index.upsert([{'id':str(UUID(int=1)),'vector':[1,2,3],'payload':{'chunk_id':str(UUID(int=1))}}])
    assert [item[0] for item in calls]==['GET','PUT','GET','PUT']


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['wrong_dimensions','unconfirmed'])
async def test_vector_mismatch_or_unconfirmed_write_is_rejected(mode):
    def handler(request):
        result={'config':{'params':{'vectors':{'size':4,'distance':'Cosine'}}},'points_count':0} if request.method=='GET' else {'status':'acknowledged'}
        return httpx.Response(200,json={'status':'ok','result':result})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        index=QdrantVectorIndex(vector_settings(),client)
        with pytest.raises(VectorIndexError):
            if mode=='wrong_dimensions': await index.ensure_collection(EmbeddingProfile('c','m','v',3))
            else: await index.upsert([{'id':str(UUID(int=1)),'vector':[1,2,3]}])


def scenario():
    raw=('会议安排的项目资料。'*900).encode()
    message=dict(job_id=UUID(int=1),tenant_id=UUID(int=2),user_id=UUID(int=3))
    job=dict(id=message['job_id'],document_id=UUID(int=4),version_id=UUID(int=5),source_text=None,
        storage_backend='local',storage_key=str(UUID(int=6)),source_bytes=len(raw),source_hash=sha256(raw).hexdigest(),
        embedding_provider='openai_compatible',embedding_model='test',embedding_model_version='v1',embedding_dimensions=3,
        category='project',visibility='private',is_sensitive=False,external_embedding_allowed=True)
    settings=SimpleNamespace(knowledge_index_lease_seconds=180,knowledge_index_max_claims=3,
        knowledge_max_source_bytes=262144,embedding_provider='openai_compatible',embedding_model='test',
        embedding_model_version='v1',embedding_dimensions=3,qdrant_collection='test',embedding_max_batch_items=10)
    repo=SimpleNamespace(claim=AsyncMock(return_value=job),load=AsyncMock(return_value=job),
        bind_space=AsyncMock(),prepare_chunks=AsyncMock(return_value=set()),renew=AsyncMock(),
        mark_batch=AsyncMock(),complete=AsyncMock(),fail=AsyncMock())
    embedding=SimpleNamespace(max_batch_items=10,ensure_configured=lambda:None,embed=AsyncMock(side_effect=lambda subject,texts:[[1,2,3] for _ in texts]))
    index=SimpleNamespace(namespace='a'*64,ensure_collection=AsyncMock(return_value={'points_count':0}),upsert=AsyncMock())
    service=KnowledgeIndexService(settings,repo,SimpleNamespace(read=AsyncMock(return_value=raw)),embedding,index)
    return service,message,job,repo,embedding,index,raw


@pytest.mark.asyncio
async def test_index_batches_ali_limit_and_only_completes_after_confirmed_writes():
    service,message,job,repo,embedding,index,raw=scenario()
    result=await service.execute(message)
    assert result['status']=='succeeded'
    assert all(len(call.args[1])<=10 for call in embedding.embed.await_args_list)
    assert index.upsert.await_count==repo.mark_batch.await_count==embedding.embed.await_count
    assert 'content' not in index.upsert.await_args.args[0][0]['payload']
    repo.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_skips_confirmed_chunks_and_duplicates_are_ignored():
    service,message,job,repo,embedding,index,raw=scenario()
    repo.prepare_chunks.return_value={item.id for item in chunk_text(raw.decode(),job['version_id'])}
    assert (await service.execute(message))['status']=='succeeded'
    embedding.embed.assert_not_awaited()
    repo.claim.return_value=None
    assert (await service.execute(message))['status']=='ignored'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['changed_source','sensitive','write_failure'])
async def test_failures_never_mark_ready(mode):
    service,message,job,repo,embedding,index,raw=scenario()
    if mode=='changed_source': job['source_hash']='b'*64
    if mode=='sensitive': job['is_sensitive']=True
    if mode=='write_failure': index.upsert.side_effect=VectorIndexError('QDRANT_WRITE_UNCONFIRMED','失败')
    assert (await service.execute(message))['status']=='failed'
    repo.complete.assert_not_awaited()
    repo.fail.assert_awaited_once()
    if mode!='write_failure': embedding.embed.assert_not_awaited()


def test_queue_accepts_only_identity_envelope():
    assert parse_message('{"job_id":"00000000-0000-0000-0000-000000000001","tenant_id":"00000000-0000-0000-0000-000000000002","user_id":"00000000-0000-0000-0000-000000000003"}')['job_id']==UUID(int=1)
    with pytest.raises(ValueError): parse_message('{"job_id":"bad","content":"instructions"}')
