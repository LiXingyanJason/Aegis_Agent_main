import asyncio
from aegis_agent_worker.harness.context import get_execution
from aegis_agent_worker.harness.exceptions import HarnessError
from .client import RerankError,validate_scores


class GovernedReranker:
    def __init__(self,settings,harness,client):
        self.settings,self.harness,self.client=settings,harness,client

    @property
    def policy(self): return get_execution().policy_bundle.rerank

    @property
    def enabled(self): return self.policy.enabled

    async def rank(self,context,query,evidence,authorize):
        runtime=get_execution();p=self.policy
        if (context.run_id,context.tenant_id,context.user_id)!=(runtime.run.id,runtime.run.tenant_id,runtime.run.user_id):
            raise HarnessError('RERANK_IDENTITY_MISMATCH','重排身份不匹配')
        if not p.enabled: raise HarnessError('RERANK_PURPOSE_DENIED','原任务策略不允许重排')
        if not self.settings.rerank_enabled or not self.settings.rerank_external_allowed:
            raise HarnessError('RERANK_EXTERNAL_BLOCKED','重排外发未开启')
        if (not query.strip() or len(query)>p.max_query_chars or not 1<=len(evidence)<=p.max_candidates
            or len({e.chunk_id for e in evidence})!=len(evidence)
            or any(not e.excerpt.strip() or len(e.excerpt)>p.max_document_chars for e in evidence)
            or len(query)+sum(len(e.excerpt) for e in evidence)>p.max_total_chars):
            raise HarnessError('RERANK_INPUT_LIMIT','重排输入超过额度或契约无效')
        # Permission callback reloads from SQL immediately before the request, not
        # the stale retrieval snapshot; external_embedding_allowed is insufficient.
        async with self.harness.attempt('rerank',self.settings.rerank_model) as audit:
            audit.update(purpose='knowledge_rerank',model_provider='dashscope',model_name=self.settings.rerank_model,
                rerank_metadata={'policy_version':p.version,'candidate_count':len(evidence),
                    'input_chars':len(query)+sum(len(e.excerpt) for e in evidence),
                    'candidates':[dict(chunk_id=str(e.chunk_id),version_id=str(e.version_id),content_hash=e.content_hash) for e in evidence]})
            if not await authorize(): raise HarnessError('RERANK_DOCUMENT_EXTERNAL_BLOCKED','资料重排外发许可失效')
            try:
                async with asyncio.timeout(p.timeout_seconds):
                    scores=await self.client.rank(query,[e.excerpt for e in evidence],p)
                return validate_scores([dict(index=i,relevance_score=s) for i,s in scores],len(evidence))
            finally:
                usage=self.client.usage
                audit.update(prompt_tokens=usage.prompt_tokens,completion_tokens=usage.completion_tokens,total_tokens=usage.total_tokens)
