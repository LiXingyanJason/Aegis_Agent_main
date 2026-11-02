from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4
import json
import httpx
import pytest
from aegis_agent_worker.harness.context import bind_execution,reset_execution
from aegis_agent_worker.harness.policy_provider import LEGACY_POLICY,with_generations
from aegis_agent_worker.harness.bootstrap import create_worker_policy_provider
from aegis_agent_worker.indexing.contracts import RawChunk,SourceScope,SourceRange
from aegis_agent_worker.agent.contracts.knowledge import RetrievedEvidence
from aegis_agent_worker.service.knowledge.generation_context_service import GenerationContextService
from aegis_agent_worker.service.knowledge.generation_index_service import batches
from aegis_agent_worker.vector.qdrant_search import QdrantSearch
from aegis_agent_worker.embedding.contracts import EmbeddingProfile
from aegis_agent_worker.tool.contracts import ToolContext
from aegis_agent_worker.vector.contracts import VectorIndexError


def test_batching_honors_enhanced_text_not_only_original_chars():
    rows=[dict(retrieval_text='x'*2200) for _ in range(10)]
    grouped=list(batches(rows,10,16000))
    assert [len(g) for g in grouped]==[7,3]
    assert all(sum(len(c['retrieval_text']) for c in g)<=16000 for g in grouped)


def test_old_policy_does_not_gain_generation_and_restore_original_profile():
    p=create_worker_policy_provider(knowledge_enabled=True,generations_enabled=True)
    assert p.current().generations.enabled
    assert not p.resolve('harness-v1').generations.enabled
    assert p.current().execution.max_models==create_worker_policy_provider(knowledge_enabled=True).current().execution.max_models


@pytest.mark.asyncio
async def test_direct_hits_survive_expansion_and_final_size_checks():
    scope=SourceScope(document_id=uuid4(),version_id=uuid4(),generation_id=uuid4(),parser_version='structure-v1')
    chunks=[];source=''
    for n in range(7):
        body=str(n)+'条件。'*149
        c=RawChunk(scope=scope,structure='clause',heading_path=('政策',),clause_id='1',
            source_range=SourceRange(start=len(source),end=len(source)+len(body)),content=body,content_hash=sha256(body.encode()).hexdigest())
        chunks.append(c);source+=body
    rows=[dict(raw=c.model_dump(mode='json'),relations=[]) for c in chunks]
    class Repo:
        async def structure_snapshot(self,*args):return rows
    evidence=[RetrievedEvidence(source_no=i+1,document_id=scope.document_id,version_id=scope.version_id,
        generation_id=scope.generation_id,chunk_id=c.chunk_id,title='政策',version_label='v1',section='政策',
        start_offset=c.source_range.start,end_offset=c.source_range.end,excerpt=c.content,content_hash=c.content_hash) for i,c in enumerate(chunks[:5])]
    token=bind_execution(SimpleNamespace(id=uuid4()),with_generations(LEGACY_POLICY))
    try:result=await GenerationContextService(Repo()).augment(None,evidence,{})
    finally:reset_execution(token)
    assert [e.chunk_id for e in result]==[e.chunk_id for e in evidence]
    assert [e.excerpt for e in result]==[e.excerpt for e in evidence]
    assert sum(len(e.excerpt)+sum(len(p.excerpt) for p in e.parts) for e in result)<=3500
    assert len(json.dumps(dict(evidence=[e.model_dump(mode='json') for e in result],retrieval={}),ensure_ascii=False).encode())<=12000


@pytest.mark.asyncio
async def test_vector_filter_uses_sql_chunk_id_allowlist():
    cid=uuid4();version=uuid4();ctx=ToolContext(uuid4(),uuid4(),uuid4())
    from pydantic import SecretStr
    settings=SimpleNamespace(qdrant_enabled=True,qdrant_url='https://vector.example',qdrant_api_key=SecretStr('fake'),
        qdrant_collection='kb',qdrant_request_timeout_seconds=1)
    requests=[]
    def handler(request):
        body=json.loads(request.content);requests.append(body)
        assert {'has_id':[str(cid)]} in body['filter']['must']
        return httpx.Response(200,json=dict(status='ok',result=dict(points=[])))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        vector=QdrantSearch(settings,http)
        profile=EmbeddingProfile('openai_compatible','fake','v1',3)
        await vector.search(ctx,[version],[1.,0.,0.],profile,5,chunk_ids=[cid])
        assert await vector.search(ctx,[version],[1.,0.,0.],profile,5,chunk_ids=[])==[]
        with pytest.raises(VectorIndexError):await vector.search(ctx,[version],[1.,0.,0.],profile,5,chunk_ids=[cid]*4097)
    assert len(requests)==1
