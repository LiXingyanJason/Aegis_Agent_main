"""检索SQL：身份/有效状态在召回前过滤，交付时再次复核；模型不能拼SQL。"""
from sqlalchemy import text
from aegis_agent_worker.config.database import TenantContext, tenant_transaction
from aegis_agent_worker.agent.contracts.knowledge import RetrievedEvidence
from aegis_agent_worker.retrieval.ranking import SearchHit


VISIBLE = """d.tenant_id=:tenant AND v.tenant_id=:tenant
 AND (d.user_id=:user OR d.visibility='tenant')
 AND d.deleted_at IS NULL AND d.enabled AND NOT d.is_sensitive
 AND d.external_embedding_allowed AND v.status='ready' AND d.current_version_id=v.id"""
JOINS = """FROM knowledge_chunks c
 JOIN knowledge_documents d ON d.id=c.document_id AND d.tenant_id=c.tenant_id
 JOIN knowledge_document_versions v ON v.id=c.version_id AND v.document_id=d.id AND v.tenant_id=d.tenant_id"""


class KnowledgeRetrievalRepository:
    def __init__(self, database):
        self._database = database

    async def _rows(self, context, sql, **params):
        async with self._database.session() as session:
            async with tenant_transaction(session, TenantContext(context.tenant_id, context.user_id)):
                return (await session.execute(text(sql), dict(tenant=context.tenant_id, user=context.user_id, **params))).mappings().all()

    async def scope(self, context, maximum):
        # LIMIT+1用于明确拒绝过大的过滤范围，不能截断后假称完整检索。
        return await self._rows(context, """SELECT v.id AS version_id,v.embedding_provider,v.embedding_model,
            v.embedding_model_version,v.embedding_dimensions,v.vector_namespace,v.vector_collection
            FROM knowledge_documents d JOIN knowledge_document_versions v ON v.document_id=d.id AND v.tenant_id=d.tenant_id
            WHERE """ + VISIBLE + " ORDER BY v.id LIMIT :maximum",
            maximum=maximum+1)

    async def check_space(self, context, namespace, collection, profile):
        rows = await self._rows(context, """SELECT namespace FROM knowledge_vector_spaces WHERE namespace=:namespace
            AND collection_name=:collection AND model_provider=:provider AND model_name=:model
            AND model_version=:version AND dimensions=:dimensions""", namespace=namespace, collection=collection,
            provider=profile.provider, model=profile.model, version=profile.version, dimensions=profile.dimensions)
        return bool(rows)

    async def lexical(self, context, versions, query, limit):
        # query由确定性词项生成；仍使用绑定参数和to_tsquery，不拼接模型SQL。
        rows = await self._rows(context, """SELECT c.id AS chunk_id,ts_rank_cd(c.search_text,to_tsquery('simple',:query)) AS score
            """ + JOINS + " WHERE " + VISIBLE + """ AND c.tenant_id=:tenant AND c.indexed_at IS NOT NULL
            AND v.id=ANY(CAST(:versions AS uuid[])) AND c.search_text @@ to_tsquery('simple',:query)
            ORDER BY score DESC,c.id LIMIT :limit""", versions=versions, query=query, limit=limit)
        return [SearchHit(row['chunk_id'], float(row['score'])) for row in rows]

    async def load(self, context, versions, ids):
        if not ids:
            return []
        rows = await self._rows(context, """SELECT c.id AS chunk_id,c.document_id,c.version_id,d.title,v.version_label,
            coalesce(nullif(c.section,''),'正文') AS section,c.start_offset,c.end_offset,left(c.content,800) AS excerpt,c.content_hash
            """ + JOINS + " WHERE " + VISIBLE + """ AND c.tenant_id=:tenant AND c.indexed_at IS NOT NULL
            AND v.id=ANY(CAST(:versions AS uuid[])) AND c.id=ANY(CAST(:ids AS uuid[]))""", versions=versions, ids=ids)
        return [RetrievedEvidence(source_no=1, **dict(row)) for row in rows]
