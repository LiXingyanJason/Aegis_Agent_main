"""权限前置、RRF、有界故障处理；替身不代表真实模型语义质量。"""
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID
import pytest
from aegis_agent_worker.agent.contracts.knowledge import KnowledgeQueryPlan,RetrievedEvidence
from aegis_agent_worker.retrieval.policy import RetrievalPolicy
from aegis_agent_worker.retrieval.ranking import SearchHit,ordered_hits,fuse
from aegis_agent_worker.service.knowledge.hybrid_retrieval_service import HybridRetrievalService
from aegis_agent_worker.tool.contracts import ToolContext,ToolError
from aegis_agent_worker.vector.contracts import VectorIndexError
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.embedding.exceptions import EmbeddingError


def settings():
    return SimpleNamespace(embedding_provider='openai_compatible',embedding_model='test',embedding_model_version='v1',
        embedding_dimensions=3,qdrant_url='https://test.example',qdrant_collection='kb',knowledge_lexical_fallback_allowed=False)


CONTEXT=ToolContext(UUID(int=1),UUID(int=10),UUID(int=600))


def evidence(number):
    return RetrievedEvidence(source_no=1,document_id=UUID(int=100),version_id=UUID(int=101),chunk_id=UUID(int=number),
        title='虚构续约政策',version_label='v1',section='正文',start_offset=0,end_offset=10,
        excerpt='续约需提前30天通知',content_hash='a'*64)


class Repository:
    def __init__(self):
        self.calls=[]
        self.rows=[dict(version_id=UUID(int=101),embedding_provider='openai_compatible',embedding_model='test',
            embedding_model_version='v1',embedding_dimensions=3,vector_namespace=sha256(b'https://test.example/kb').hexdigest(),
            vector_collection='kb')]
        self.hits=[SearchHit(UUID(int=201),2),SearchHit(UUID(int=202),1)]
        self.fresh=[evidence(201),evidence(202),evidence(203)]
    async def scope(self,context,maximum):
        self.calls.append(('scope',context,maximum))
        return self.rows
    async def check_space(self,*args): return True
    async def lexical(self,context,versions,query,limit):
        self.calls.append(('lexical',versions,query,limit))
        return self.hits
    async def load(self,context,versions,ids):
        self.calls.append(('load',versions,ids))
        return [item for item in self.fresh if item.chunk_id in ids]


class Embedding:
    def __init__(self,error=None): self.calls=[];self.error=error
    async def embed(self,subject,queries):
        self.calls.append((subject,queries))
        if self.error: raise self.error
        return [[1.,0.,0.] for _ in queries]


class Vector:
    def __init__(self,error=None): self.calls=[];self.error=error
    async def search(self,context,versions,vector,profile,limit):
        self.calls.append((context,versions,limit))
        if self.error: raise self.error
        return [SearchHit(UUID(int=203),0.9),SearchHit(UUID(int=202),0.8)]


def system(error=None,policy=None):
    repo,embedding,vector=Repository(),Embedding(),Vector(error)
    return HybridRetrievalService(settings(),repo,embedding,vector,policy),repo,embedding,vector


def test_rrf_duplicates_ties_are_stable_and_not_probability():
    a,b,c=[SearchHit(UUID(int=n),1) for n in (1,2,3)]
    assert ordered_hits([b,a,a],20)==[a,b]
    result=fuse([a,b,a],[c,b],60)
    assert [item[0].int for item in result]==[2,1,3]
    assert result[0][1]==pytest.approx(2/62)
    assert result[0][2]=={'lexical':2,'vector':2}


@pytest.mark.asyncio
async def test_hybrid_safe_scope_rewrite_dedup_and_stable_order():
    service,repo,embedding,vector=system()
    plan=KnowledgeQueryPlan(query='续签要提前多久',query_rewrites=['续约通知期限'])
    result=await service.search(CONTEXT,plan)
    assert [item.chunk_id.int for item in result.evidence]==[202,201,203]
    assert [item.source_no for item in result.evidence]==[1,2,3]
    assert result.retrieval.mode=='hybrid' and not result.retrieval.degraded
    assert len(embedding.calls)==1 and embedding.calls[0][0].purpose=='knowledge_query'
    assert embedding.calls[0][0].id==CONTEXT.run_id
    assert len(vector.calls)==2 and all(call[1]==[UUID(int=101)] for call in vector.calls)
    assert repo.calls[0][0]=='scope'
    assert '续约' in repo.calls[1][2]
    assert await service.search(CONTEXT,plan)==result


@pytest.mark.asyncio
async def test_empty_scope_no_cloud_calls():
    service,repo,embedding,vector=system()
    repo.rows=[]
    result=await service.search(CONTEXT,{'query':'政策'})
    assert result.evidence==[] and result.retrieval.reason=='no_accessible_documents'
    assert not embedding.calls and not vector.calls and len(repo.calls)==1


@pytest.mark.asyncio
async def test_scope_limit_rejects_without_truncation_or_cloud():
    service,repo,embedding,vector=system(policy=RetrievalPolicy(max_versions=1))
    repo.rows*=2
    with pytest.raises(ToolError,match='范围过大'): await service.search(CONTEXT,{'query':'政策'})
    assert not embedding.calls and not vector.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('embedding_model','other'),('vector_namespace','other'),('vector_collection','other')])
async def test_profile_mismatch_never_queries(field,value):
    service,repo,embedding,vector=system()
    repo.rows[0][field]=value
    with pytest.raises(ToolError): await service.search(CONTEXT,{'query':'政策'})
    assert not embedding.calls and not vector.calls


@pytest.mark.asyncio
async def test_default_vector_failure_not_empty_and_not_fallback():
    service,*_=system(VectorIndexError('QDRANT_UNAVAILABLE','不可用'))
    with pytest.raises(ToolError) as error: await service.search(CONTEXT,{'query':'政策'})
    assert error.value.code=='QDRANT_UNAVAILABLE'


@pytest.mark.asyncio
async def test_explicit_fallback_marked_and_does_not_keep_partial_vector_results():
    service,repo,embedding,vector=system(VectorIndexError('QDRANT_UNAVAILABLE','不可用'),RetrievalPolicy(allow_lexical_fallback=True))
    calls=[]
    async def partial(*args):
        calls.append(args)
        if len(calls)==1:
            return [SearchHit(UUID(int=203),0.99)]
        raise VectorIndexError('QDRANT_UNAVAILABLE','第二条改写查询失败')
    vector.search=partial
    result=await service.search(CONTEXT,{'query':'政策','query_rewrites':['续约']})
    assert len(calls)==2
    assert [item.chunk_id.int for item in result.evidence]==[201,202]
    assert result.retrieval.mode=='lexical_only' and result.retrieval.degraded
    assert result.retrieval.reason=='vector_unavailable'
    assert all(item.vector_rank is None for item in result.retrieval.rankings)


@pytest.mark.asyncio
@pytest.mark.parametrize('code',['QDRANT_TIMEOUT','QDRANT_NOT_CONFIGURED','QDRANT_QUERY_REJECTED','QDRANT_AUTH_FAILED'])
async def test_non_transient_or_timeout_error_cannot_fallback(code):
    service,*_=system(VectorIndexError(code,'失败'),RetrievalPolicy(allow_lexical_fallback=True))
    with pytest.raises(ToolError) as error: await service.search(CONTEXT,{'query':'政策'})
    assert error.value.code==code


@pytest.mark.asyncio
@pytest.mark.parametrize('error',[HarnessError('EMBEDDING_BUDGET_EXCEEDED','预算不足'),
    HarnessError('EMBEDDING_DATA_POLICY_BLOCKED','未授权'),EmbeddingError('EMBEDDING_UNAVAILABLE','网络错误')])
async def test_embedding_governance_never_fallback_or_retry(error):
    service,repo,embedding,vector=system(policy=RetrievalPolicy(allow_lexical_fallback=True))
    embedding.error=error
    with pytest.raises(ToolError) as raised: await service.search(CONTEXT,{'query':'政策'})
    assert raised.value.code==error.code and len(embedding.calls)==1 and not vector.calls


@pytest.mark.asyncio
async def test_revoked_candidates_removed_and_numbers_contiguous():
    service,repo,embedding,vector=system()
    repo.fresh=[evidence(203)]
    result=await service.search(CONTEXT,{'query':'政策'})
    assert len(result.evidence)==1 and result.evidence[0].chunk_id.int==203 and result.evidence[0].source_no==1
    repo.fresh=[]
    result=await service.search(CONTEXT,{'query':'政策'})
    assert result.retrieval.reason=='sources_unavailable' and result.evidence==[]


@pytest.mark.asyncio
async def test_no_matches_different_from_failure():
    service,repo,embedding,vector=system()
    repo.hits=[]
    async def empty(*args): return []
    vector.search=empty
    result=await service.search(CONTEXT,{'query':'政策'})
    assert result.evidence==[] and result.retrieval.reason=='no_matches'


@pytest.mark.asyncio
async def test_invalid_plan_and_identity_cannot_enter_repository():
    service,repo,embedding,vector=system()
    with pytest.raises(ValueError): await service.search(CONTEXT,{'query':'政策','tenant_id':'fake'})
    with pytest.raises(ToolError): await service.search(SimpleNamespace(tenant_id=None),{'query':'政策'})
    assert not repo.calls


@pytest.mark.asyncio
async def test_result_byte_limit_drops_tail_without_changing_evidence():
    service,repo,embedding,vector=system(policy=RetrievalPolicy(max_result_bytes=2500))
    repo.fresh=[item.model_copy(update={'excerpt':'中'*700}) for item in repo.fresh]
    result=await service.search(CONTEXT,{'query':'政策'})
    assert not result.evidence and result.retrieval.reason=='result_limit'


@pytest.mark.asyncio
async def test_query_terms_over_limit_never_embeddings():
    service,repo,embedding,vector=system(policy=RetrievalPolicy(max_query_terms=1))
    with pytest.raises(ToolError) as error:
        await service.search(CONTEXT,{'query':'续约通知期限'})
    assert error.value.code=='KNOWLEDGE_QUERY_LIMIT' and not embedding.calls and not vector.calls


@pytest.mark.asyncio
async def test_bootstrap_gateway_invokes_injected_hybrid_with_trusted_context():
    from aegis_agent_worker.config.settings import Settings
    from aegis_agent_worker.tool.bootstrap import create_default_tool_gateway
    service,repo,embedding,vector=system()
    config=Settings(_env_file=None,database_url='postgresql://unused/unused',redis_url='redis://unused',
        model_provider='openai',model_api_base='https://example.invalid',model_api_key='offline',model_default_name='offline')
    gateway=create_default_tool_gateway(config,knowledge_retrieval=service)
    prepared=await gateway.prepare(None,tenant_id=CONTEXT.tenant_id,user_id=CONTEXT.user_id,run_id=CONTEXT.run_id,
        invocation=KnowledgeQueryPlan(query='续约').to_invocation())
    result=await gateway.invoke(prepared)
    assert result.response_payload['retrieval']['policy_version']=='hybrid-v1'
    assert result.output_summary['evidence_count']==3
    assert repo.calls[0][1]==CONTEXT and prepared.context.connection_id is None
