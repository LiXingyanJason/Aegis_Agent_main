"""权限范围→全文/受治理向量→RRF→SQL复核；不调用聊天LLM。"""
from hashlib import sha256
import json
from uuid import UUID
from aegis_agent_worker.agent.contracts.knowledge import KnowledgeQueryPlan, KnowledgeSearchResult
from aegis_agent_worker.embedding.contracts import EmbeddingProfile
from aegis_agent_worker.embedding.exceptions import EmbeddingError
from aegis_agent_worker.harness.embedding_executor import EmbeddingSubject
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.indexing.chunker import search_tokens
from aegis_agent_worker.retrieval.policy import RetrievalPolicy
from aegis_agent_worker.retrieval.ranking import ordered_hits, fuse
from aegis_agent_worker.tool.contracts import ToolError
from aegis_agent_worker.vector.contracts import VectorIndexError


class HybridRetrievalService:
    def __init__(self, settings, repository, embedding, vector, policy=None):
        self._settings, self._repository = settings, repository
        self._embedding, self._vector = embedding, vector
        self._policy = policy or RetrievalPolicy(allow_lexical_fallback=settings.knowledge_lexical_fallback_allowed)

    async def search(self, context, plan):
        """Hybrid 检索 实现"""
        # 验证 KnowledgeQueryPlan，要求 context 中有 UUID 格式的租户、用户和任务 ID
        plan = KnowledgeQueryPlan.model_validate(plan.model_dump() if isinstance(plan,KnowledgeQueryPlan) else plan)
        if any(not isinstance(getattr(context,name,None),UUID) for name in ('tenant_id','user_id','run_id')):
            raise ToolError('KNOWLEDGE_CONTEXT_REQUIRED','缺少可信检索任务身份')
        # 从 PostgreSQL 取得当前用户可访问的有效版本。范围为空就返回空证据；超过策略上限就明确报错。
        p, s = self._policy, self._settings
        # 初始化本次检索的诊断信息：策略版本、默认模式 hybrid、尚未降级、尚无空结果原因，也没有排名记录。
        metadata = dict(policy_version=p.version, mode='hybrid', degraded=False, reason=None, rankings=[])
        scope = await self._repository.scope(context,p.max_versions) # 先从 PostgreSQL 找出当前用户有权检索的、已启用且当前版本已就绪的资料版本。
        if len(scope)>p.max_versions: # 可访问版本超过策略上限
            raise ToolError('KNOWLEDGE_SCOPE_TOO_LARGE','可访问资料范围过大，请缩小查询范围')
        if not scope:
            return KnowledgeSearchResult(retrieval={**metadata,'reason':'no_accessible_documents'})
        # 从当前配置构造查询所使用的模型空间：供应商、模型、模型版本和向量维度
        profile = EmbeddingProfile(s.embedding_provider,s.embedding_model,s.embedding_model_version,s.embedding_dimensions)
        # 根据 Qdrant 地址和集合名计算稳定标识，用来核对资料当初绑定的是不是当前这个向量集合
        namespace = sha256((str(s.qdrant_url).rstrip('/')+'/'+s.qdrant_collection).encode()).hexdigest()
        for row in scope:
            # 对每个可检索版本检查：它的 Embedding 模型及维度是否与当前 profile 相同；
            # 它绑定的 Qdrant namespace 和 collection 是否与当前配置相同。不一致就拒绝检索，避免混用不同向量空间。
            if (row['embedding_provider'],row['embedding_model'],row['embedding_model_version'],row['embedding_dimensions']) != (
                profile.provider,profile.model,profile.version,profile.dimensions):
                raise ToolError('EMBEDDING_PROFILE_MISMATCH','可访问资料包含不同模型空间，不能混合检索')
            if row['vector_namespace']!=namespace or row['vector_collection']!=s.qdrant_collection:
                raise ToolError('QDRANT_PROFILE_MISMATCH','资料与查询集合绑定不一致')
        if not await self._repository.check_space(context,namespace,s.qdrant_collection,profile):
            raise ToolError('QDRANT_PROFILE_MISMATCH','集合尚未绑定当前模型空间')
        versions = [row['version_id'] for row in scope]
        # 本次要用于检索的查询文本列表，由用户问题对应的主查询和 LLM 生成的改写词组成
        # 一次用户提问可以对应 1～3 条查询文本：一条主查询，加上最多两条改写。
        # 改写词由 KnowledgeAgent 在生成 KnowledgeQueryPlan 时提出，并经 Schema 校验
        queries = list(dict.fromkeys([plan.query,*plan.query_rewrites]))
        terms = list(dict.fromkeys(search_tokens(' '.join(queries)).split()))
        if len(terms)>p.max_query_terms:
            raise ToolError('KNOWLEDGE_QUERY_LIMIT','检索词项过多，请缩短问题或改写')
        # 固定分词器不产生引号/SQL字符；仍引用词项并使用SQL绑定参数。
        tsquery = ' | '.join("'"+term.replace("'","''")+"'" for term in terms)
        # 有检索词项时，执行关键词检索SQL并整理候选；没有词项时，返回空列表
        lexical = ordered_hits(await self._repository.lexical(context,versions,tsquery,p.lexical_candidates),p.lexical_candidates) if terms else []
        vector_hits=[]
        try:
            # 把 queries 中的每条查询文本变成向量
            vectors = await self._embedding.embed(EmbeddingSubject('run',context.run_id,context.tenant_id,context.user_id),queries)
            if len(vectors)!=len(queries):
                raise ToolError('EMBEDDING_INVALID_RESPONSE','查询向量数量不一致')
            for vector in vectors:
                # Qdrant 查找相似片段
                vector_hits.extend(await self._vector.search(context,versions,vector,profile,p.vector_candidates))
        except (HarnessError, EmbeddingError) as error:
            # 预算/外发策略/Embedding网络失败不能通过降级绕过治理。
            raise ToolError(error.code,str(error)) from None
        except VectorIndexError as error:
            if not p.allow_lexical_fallback or error.code!='QDRANT_UNAVAILABLE':
                raise ToolError(error.code,str(error)) from None
            metadata.update(mode='lexical_only',degraded=True,reason='vector_unavailable')
            vector_hits=[]
        # 多条查询向量可能命中同一个片段。
        # ordered_hits 按 chunk_id 去重，保留该片段的最高分，再按分数排序
        vector_hits = ordered_hits(vector_hits,p.vector_candidates)
        ranking = fuse(lexical,vector_hits,p.rrf_k)
        # 把关键词检索 lexical 和向量检索 vector_hits 的排名用 RRF 融合。
        fresh = {item.chunk_id:item for item in await self._repository.load(context,versions,[item[0] for item in ranking])}
        # 把所有融合候选的 ID 交回 PostgreSQL，按当前用户权限、有效版本和索引状态重新读取。
        evidence=[]
        for chunk_id, score, ranks in ranking:
            if chunk_id not in fresh:
                continue
            if len(evidence)>=plan.top_k:
                break
            evidence.append(fresh[chunk_id].model_copy(update={'source_no':len(evidence)+1}))
            metadata['rankings'].append(dict(chunk_id=chunk_id,rrf_score=score,
                lexical_rank=ranks.get('lexical'),vector_rank=ranks.get('vector')))
        if not evidence and metadata['reason'] is None:
            metadata['reason']='sources_unavailable' if ranking else 'no_matches'
        # 同时遵守结构化State总字节限制。只删除末位证据，绝不截断/篡改原文或hash。
        while evidence and len(json.dumps({'evidence':[item.model_dump(mode='json') for item in evidence],
            'retrieval':metadata},ensure_ascii=False,default=str).encode('utf-8'))>p.max_result_bytes:
            evidence.pop()
            metadata['rankings'].pop()
        if not evidence and ranking and metadata['reason'] is None:
            metadata['reason']='result_limit'
        return KnowledgeSearchResult(evidence=evidence,retrieval=metadata)
