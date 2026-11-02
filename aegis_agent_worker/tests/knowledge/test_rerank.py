from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID
import httpx
import pytest
from pydantic import SecretStr
from aegis_agent_worker.rerank.client import RerankClient,RerankError,validate_scores
from aegis_agent_worker.rerank.executor import GovernedReranker
from aegis_agent_worker.harness.policies.rerank import RerankPolicy
from aegis_agent_worker.harness.context import bind_execution,reset_execution
from aegis_agent_worker.harness.policy_provider import LEGACY_POLICY,with_rerank
from aegis_agent_worker.harness.bootstrap import create_worker_policy_provider
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.common.usage import TokenUsage
from test_hybrid_retrieval import Repository,Embedding,Vector,settings,CONTEXT,evidence
from aegis_agent_worker.service.knowledge.hybrid_retrieval_service import HybridRetrievalService
from aegis_agent_worker.agent.contracts.knowledge import KnowledgeQueryPlan


def config():
    return SimpleNamespace(rerank_enabled=True,rerank_external_allowed=True,rerank_api_url='https://rerank.example/api',
        rerank_api_key=SecretStr('fake'),rerank_model='qwen3.7-text-rerank')


def test_default_model_is_qwen_37_without_enabling_external_calls(monkeypatch):
    from test_qdrant_settings import configured
    for name in ('RERANK_MODEL','RERANK_ENABLED','RERANK_EXTERNAL_ALLOWED'):
        monkeypatch.delenv(name,raising=False)
    settings=configured()
    assert settings.rerank_model=='qwen3.7-text-rerank'
    assert not settings.rerank_enabled and not settings.rerank_external_allowed


@pytest.mark.parametrize('model',['qwen3.7-text-rerank','qwen3-rerank','unregistered-reranker'])
def test_enabled_model_allowlist(model):
    from test_qdrant_settings import configured
    values=dict(rerank_enabled=True,rerank_model=model,rerank_api_url='https://rerank.example/api',rerank_api_key='fake')
    if model=='unregistered-reranker':
        with pytest.raises(ValueError):configured(**values)
    else:assert configured(**values).rerank_model==model


@pytest.mark.parametrize('rows', [[],[dict(index=True,relevance_score=.5)],
    [dict(index=1,relevance_score=.5)],[dict(index=0,relevance_score=float('nan'))],
    [dict(index=0,relevance_score=2)],[dict(index=0,relevance_score='0.5')]])
def test_invalid_response(rows):
    with pytest.raises(RerankError): validate_scores(rows,1)


def test_duplicate_indices_and_invalid_policy_rejected():
    with pytest.raises(RerankError): validate_scores([dict(index=0,relevance_score=.5)]*2,2)
    with pytest.raises(ValueError): RerankPolicy(max_candidates=100)


@pytest.mark.asyncio
@pytest.mark.parametrize('model',['qwen3.7-text-rerank','qwen3-rerank'])
async def test_dashscope_protocol_and_usage_ties(model):
    def handler(request):
        import json
        data=json.loads(request.content)
        assert data['model']==model
        assert data['input']==dict(query='问题',documents=['原文A','原文B'])
        assert data['parameters']==(dict(top_n=2) if model=='qwen3.7-text-rerank' else dict(top_n=2,return_documents=False))
        usage=dict(prompt_tokens=13,total_tokens=13) if model=='qwen3.7-text-rerank' else dict(total_tokens=13)
        return httpx.Response(200,json=dict(output=dict(results=[dict(index=1,relevance_score=.5),dict(index=0,relevance_score=.5)]),usage=usage))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        settings=config();settings.rerank_model=model
        client=RerankClient(settings,http)
        assert await client.rank('问题',['原文A','原文B'],RerankPolicy())==[(0,.5),(1,.5)]
        assert client.usage==TokenUsage(prompt_tokens=13 if model=='qwen3.7-text-rerank' else None,total_tokens=13)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code',[(429,'RERANK_RATE_LIMITED'),(503,'RERANK_UNAVAILABLE'),(302,'RERANK_UNAVAILABLE')])
async def test_http_failures_no_redirect(status,code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(status))) as http:
        with pytest.raises(RerankError) as error: await RerankClient(config(),http).rank('q',['a'],RerankPolicy())
        assert error.value.code==code


@pytest.mark.asyncio
async def test_oversize_response_and_missing_usage_remain_unknown():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'x'*3000))) as http:
        client=RerankClient(config(),http)
        with pytest.raises(RerankError): await client.rank('q',['a'],RerankPolicy(max_response_bytes=1024))
        assert client.usage.total_tokens is None


class FakeHarness:
    def __init__(self): self.calls=0;self.audits=[]
    @asynccontextmanager
    async def attempt(self,kind,label):
        assert kind=='rerank'
        self.calls+=1;audit={};self.audits.append(audit)
        yield audit


class Client:
    usage=TokenUsage()
    def __init__(self): self.calls=0
    async def rank(self,query,documents,policy):
        self.calls+=1
        return [(i,1-i/len(documents)) for i in reversed(range(len(documents)))]


@pytest.mark.asyncio
async def test_governance_identity_permission_purpose_no_recursive_memory():
    runtime=SimpleNamespace(id=CONTEXT.run_id,tenant_id=CONTEXT.tenant_id,user_id=CONTEXT.user_id)
    token=bind_execution(runtime,with_rerank(LEGACY_POLICY))
    client=Client();harness=FakeHarness();r=GovernedReranker(config(),harness,client)
    async def yes():return True
    async def no():return False
    try:
        assert await r.rank(CONTEXT,'问题',[evidence(201)],yes)==[(0,1.)]
        assert harness.audits[0]['purpose']=='knowledge_rerank'
        assert 'excerpt' not in str(harness.audits)
        with pytest.raises(HarnessError): await r.rank(CONTEXT,'问题',[evidence(201)],no)
        assert client.calls==1
        r.settings.rerank_external_allowed=False
        with pytest.raises(HarnessError): await r.rank(CONTEXT,'问题',[evidence(201)],yes)
        assert harness.calls==2
    finally: reset_execution(token)
    provider=create_worker_policy_provider(knowledge_enabled=True,rerank_enabled=True)
    assert provider.current().rerank.enabled
    assert not provider.resolve('harness-v1').rerank.enabled
    assert provider.current().execution.max_models==create_worker_policy_provider(knowledge_enabled=True).current().execution.max_models


class Reranker:
    enabled=True
    def __init__(self,error=None,fallback=False):self.error=error;self.policy=RerankPolicy(enabled=True,allow_fallback=fallback);self.calls=0
    async def rank(self,context,query,items,authorize):
        self.calls+=1
        if self.error: raise self.error
        return [(i,.9-i*.1) for i in reversed(range(len(items)))]


@pytest.mark.asyncio
async def test_reorder_before_topk_and_retain_rrf_rank():
    repo=Repository();r=Reranker()
    svc=HybridRetrievalService(settings(),repo,Embedding(),Vector(),reranker=r)
    result=await svc.search(CONTEXT,KnowledgeQueryPlan(query='续约',top_k=1))
    assert r.calls==1 and result.evidence[0].chunk_id==UUID(int=203)
    assert result.retrieval.rankings[0].rrf_rank==3 and result.retrieval.rerank_applied
    assert sum(c[0]=='load' for c in repo.calls)==2


@pytest.mark.asyncio
async def test_fallback_only_supplier_failure_not_governance():
    for error,allowed in [(RerankError('RERANK_UNAVAILABLE','安全错误'),True),(HarnessError('EXECUTION_BUDGET_EXCEEDED','预算'),False)]:
        svc=HybridRetrievalService(settings(),Repository(),Embedding(),Vector(),reranker=Reranker(error,True))
        if allowed:
            result=await svc.search(CONTEXT,KnowledgeQueryPlan(query='续约'))
            assert not result.retrieval.rerank_applied and result.retrieval.rerank_reason=='RERANK_UNAVAILABLE'
        else:
            with pytest.raises(HarnessError): await svc.search(CONTEXT,KnowledgeQueryPlan(query='续约'))


@pytest.mark.asyncio
async def test_revocation_during_rerank_never_delivered():
    repo=Repository()
    r=Reranker()
    original=r.rank
    async def revoke(*args):
        scores=await original(*args)
        repo.fresh=[]
        return scores
    r.rank=revoke
    result=await HybridRetrievalService(settings(),repo,Embedding(),Vector(),reranker=r).search(CONTEXT,KnowledgeQueryPlan(query='续约'))
    assert not result.evidence
