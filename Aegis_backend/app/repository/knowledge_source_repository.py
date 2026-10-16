"""历史来源卡片的即时权限复核，不凭Artifact旧摘录授权。"""
from sqlalchemy import text


class KnowledgeSourceRepository:
    async def load_visible(self,session,user,ids):
        if not ids:
            return []
        result=await session.execute(text("""SELECT c.id AS chunk_id,c.document_id,c.version_id,d.title,v.version_label,
            coalesce(nullif(c.section,''),'正文') AS section,c.start_offset,c.end_offset,left(c.content,800) AS excerpt,c.content_hash
            FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id AND d.tenant_id=c.tenant_id
            JOIN knowledge_document_versions v ON v.id=c.version_id AND v.document_id=d.id AND v.tenant_id=d.tenant_id
            WHERE c.id=ANY(CAST(:ids AS uuid[])) AND c.tenant_id=:tenant AND d.tenant_id=:tenant
              AND (d.user_id=:user OR d.visibility='tenant') AND d.deleted_at IS NULL AND d.enabled
              AND NOT d.is_sensitive AND d.external_embedding_allowed AND c.indexed_at IS NOT NULL
              AND v.status='ready' AND d.current_version_id=v.id"""),
            dict(ids=ids,tenant=user.tenant_id,user=user.id))
        return result.mappings().all()
