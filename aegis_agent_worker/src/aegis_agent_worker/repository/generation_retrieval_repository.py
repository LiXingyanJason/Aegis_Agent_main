from contextvars import ContextVar
from aegis_agent_worker.harness.context import get_execution
from aegis_agent_worker.agent.contracts.knowledge import RetrievedEvidence
from aegis_agent_worker.retrieval.ranking import SearchHit
from aegis_agent_worker.tool.contracts import ToolError
from .knowledge_retrieval_repository import KnowledgeRetrievalRepository,VISIBLE

JOINS='''FROM knowledge_runtime_chunks c JOIN knowledge_documents d ON d.id=c.document_id AND d.tenant_id=c.tenant_id
 JOIN knowledge_document_versions v ON v.id=c.version_id AND v.document_id=d.id AND v.tenant_id=d.tenant_id'''
FIELDS='''c.id AS chunk_id,c.document_id,c.version_id,d.title,v.version_label,
 coalesce(nullif(c.section,''),'正文') AS section,c.start_offset,c.end_offset,left(c.content,800) AS excerpt,c.content_hash,c.generation_id'''


class GenerationRetrievalRepository(KnowledgeRetrievalRepository):
    def __init__(self,database):
        super().__init__(database);self.pinned=ContextVar('generation_retrieval_ids',default=None)

    @property
    def enabled(self):return get_execution().policy_bundle.generations.enabled

    async def scope(self,context,maximum):
        rows=await super().scope(context,maximum)
        if self.enabled and len(rows)<=maximum:
            limit=get_execution().policy_bundle.generations.max_filter_ids
            ids=await self._rows(context,'SELECT c.id '+JOINS+' WHERE '+VISIBLE+''' AND c.indexed_at IS NOT NULL
              ORDER BY c.id LIMIT :limit''',limit=limit+1)
            if len(ids)>limit:raise ToolError('KNOWLEDGE_SCOPE_TOO_LARGE','代次候选过滤范围过大，禁止省略过滤')
            self.pinned.set((context.run_id,tuple(r['id'] for r in ids)))
        return rows

    def filter_ids(self,context):
        pin=self.pinned.get()
        if not pin or pin[0]!=context.run_id:raise ToolError('GENERATION_SNAPSHOT_REQUIRED','缺少可信代次快照')
        return list(pin[1])

    async def lexical(self,context,versions,query,limit):
        if not self.enabled:return await super().lexical(context,versions,query,limit)
        rows=await self._rows(context,"SELECT c.id AS chunk_id,ts_rank_cd(c.search_text,to_tsquery('simple',:query)) AS score "+JOINS+
            ' WHERE '+VISIBLE+''' AND c.id=ANY(CAST(:ids AS uuid[])) AND v.id=ANY(CAST(:versions AS uuid[]))
              AND c.indexed_at IS NOT NULL AND c.search_text @@ to_tsquery('simple',:query)
              ORDER BY score DESC,c.id LIMIT :limit''',ids=self.filter_ids(context),versions=versions,query=query,limit=limit)
        return [SearchHit(r['chunk_id'],float(r['score'])) for r in rows]

    async def load(self,context,versions,ids):
        if not self.enabled:return await super().load(context,versions,ids)
        selected=list(set(ids)&set(self.filter_ids(context)))
        if not selected:return []
        rows=await self._rows(context,'SELECT '+FIELDS+' '+JOINS+' WHERE '+VISIBLE+'''
          AND c.id=ANY(CAST(:ids AS uuid[])) AND v.id=ANY(CAST(:versions AS uuid[])) AND c.indexed_at IS NOT NULL''',ids=selected,versions=versions)
        return [RetrievedEvidence(source_no=1,**{k:v for k,v in dict(r).items() if k!='generation_id' or v is not None}) for r in rows]

    async def rerank_authorized(self,context,evidence):
        if not self.enabled:return await super().rerank_authorized(context,evidence)
        rows=await self._rows(context,'SELECT c.id,c.version_id,c.content_hash '+JOINS+' WHERE '+VISIBLE+'''
          AND c.indexed_at IS NOT NULL AND d.external_rerank_allowed AND c.id=ANY(CAST(:ids AS uuid[]))''',ids=[e.chunk_id for e in evidence])
        return {(r['id'],r['version_id'],r['content_hash']) for r in rows}=={(e.chunk_id,e.version_id,e.content_hash) for e in evidence}

    async def structure_snapshot(self,context,generation_id):
        limit=get_execution().policy_bundle.generations.max_snapshot_chunks
        rows=await self._rows(context,'''SELECT c.payload FROM knowledge_generation_chunks c
          JOIN knowledge_generation_builds b ON b.generation_id=c.generation_id AND b.status='ready'
          JOIN knowledge_index_generations g ON g.id=c.generation_id AND g.tenant_id=c.tenant_id
          JOIN knowledge_documents d ON d.id=c.document_id AND d.tenant_id=c.tenant_id
          JOIN knowledge_document_versions v ON v.id=c.version_id AND v.document_id=d.id AND v.tenant_id=d.tenant_id
          WHERE '''+VISIBLE+''' AND g.source_hash=v.content_hash AND c.generation_id=:generation ORDER BY c.position LIMIT :limit''',generation=generation_id,limit=limit+1)
        if len(rows)>limit:raise ToolError('GENERATION_CONTEXT_LIMIT','结构快照过大')
        return [r['payload'] for r in rows]
