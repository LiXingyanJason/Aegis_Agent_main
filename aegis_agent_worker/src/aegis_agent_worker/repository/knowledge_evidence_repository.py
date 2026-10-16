"""真实 SQL 权限／版本复核；Hybrid召回SQL独立于knowledge_retrieval_repository。"""
from sqlalchemy import text
from aegis_agent_worker.config.database import TenantContext, tenant_transaction
from aegis_agent_worker.agent.contracts.knowledge import KnowledgeSearchResult, RetrievedEvidence


class KnowledgeEvidenceRepository:
    def __init__(self, database):
        self._database = database

    async def revalidate(self, context, evidence):
        original = KnowledgeSearchResult(evidence=evidence)
        if not original.evidence:
            return []
        prior = {item.chunk_id: item for item in original.evidence}
        async with self._database.session() as session:
            async with tenant_transaction(session, TenantContext(context.tenant_id, context.user_id)):
                rows = (await session.execute(text("""SELECT c.id AS chunk_id,c.document_id,c.version_id,
                    d.title,v.version_label,coalesce(nullif(c.section,''),'正文') AS section,c.start_offset,c.end_offset,
                    left(c.content,800) AS excerpt,c.content_hash
                    FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id AND d.tenant_id=c.tenant_id
                    JOIN knowledge_document_versions v ON v.id=c.version_id AND v.document_id=d.id AND v.tenant_id=d.tenant_id
                    WHERE c.id=ANY(CAST(:ids AS uuid[])) AND c.tenant_id=:tenant AND d.tenant_id=:tenant
                      AND (d.user_id=:user OR d.visibility='tenant') AND d.deleted_at IS NULL AND d.enabled
                      AND NOT d.is_sensitive AND d.external_embedding_allowed
                      AND v.status='ready' AND d.current_version_id=v.id AND c.indexed_at IS NOT NULL"""),
                    dict(ids=list(prior), tenant=context.tenant_id, user=context.user_id))).mappings().all()
        current = [RetrievedEvidence(source_no=prior[row["chunk_id"]].source_no, **dict(row)) for row in rows]
        # 文档更新、正文或标题变更都不替换旧引用；返回仍精确匹配的证据。
        return [item for item in current if prior[item.chunk_id] == item]
