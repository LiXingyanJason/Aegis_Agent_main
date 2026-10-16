"""资料管理及受授权来源读取SQL；不访问存储文件或远端向量库。"""
from sqlalchemy import text


class KnowledgeDocumentRepository:
    async def detail(self,session,user,document_id):
        row=(await session.execute(text("""SELECT d.id AS document_id,d.title,d.visibility,d.enabled,
          d.is_sensitive,d.external_embedding_allowed,(d.user_id=:user) AS can_manage,d.created_at,
          v.id AS version_id,v.version_label,j.id AS job_id,
          CASE WHEN v.status='ready' THEN 'ready' WHEN j.status='succeeded' THEN 'unavailable' WHEN j.id IS NOT NULL THEN j.status
            WHEN v.status='indexing' THEN 'running' WHEN v.status='retired' THEN 'unavailable'
            WHEN v.status='failed' THEN 'failed' ELSE 'not_indexed' END AS index_status,
          (SELECT count(*) FROM knowledge_chunks c WHERE c.version_id=v.id AND c.tenant_id=d.tenant_id AND c.indexed_at IS NOT NULL) AS indexed_chunks
          FROM knowledge_documents d JOIN knowledge_document_versions v ON v.document_id=d.id AND v.tenant_id=d.tenant_id AND v.version_no=1
          LEFT JOIN knowledge_index_jobs j ON j.version_id=v.id AND j.tenant_id=d.tenant_id AND j.user_id=:user
          WHERE d.id=:id AND d.tenant_id=:tenant AND (d.user_id=:user OR d.visibility='tenant') AND d.deleted_at IS NULL"""),
          dict(id=document_id,tenant=user.tenant_id,user=user.id))).mappings().first()
        return dict(row) if row else None

    async def delete(self,session,user,document_id):
        # 先撤销SQL访问；不删源文件/审计/向量，也不主动重建或取消远端调用。
        return (await session.execute(text("""UPDATE knowledge_documents SET deleted_at=now(),enabled=false,updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND deleted_at IS NULL"""),
          dict(id=document_id,tenant=user.tenant_id,user=user.id))).rowcount>0

    async def chunk(self,session,user,document_id,version_id,chunk_id,content_hash):
        row=(await session.execute(text("""SELECT c.id AS chunk_id,c.document_id,c.version_id,d.title,v.version_label,
          coalesce(nullif(c.section,''),'正文') AS section,c.start_offset,c.end_offset,left(c.content,800) AS excerpt,c.content_hash
          FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id AND d.tenant_id=c.tenant_id
          JOIN knowledge_document_versions v ON v.id=c.version_id AND v.document_id=d.id AND v.tenant_id=d.tenant_id
          WHERE c.id=:chunk AND c.document_id=:document AND c.version_id=:version AND c.content_hash=:hash
            AND c.tenant_id=:tenant AND d.tenant_id=:tenant AND (d.user_id=:user OR d.visibility='tenant')
            AND d.deleted_at IS NULL AND d.enabled AND NOT d.is_sensitive AND d.external_embedding_allowed
            AND v.status='ready' AND d.current_version_id=v.id AND c.indexed_at IS NOT NULL"""),
          dict(chunk=chunk_id,document=document_id,version=version_id,hash=content_hash,tenant=user.tenant_id,user=user.id))).mappings().first()
        return dict(row) if row else None
