"""知识资料权限事实 SQL；不接受正文或分类标签声明的 ACL。"""
from sqlalchemy import text


class KnowledgeRepository:
    async def get_document(self, session, *, tenant_id, user_id, document_id):
        result = await session.execute(text("""SELECT id,title,category,visibility,user_id,current_version_id,
            is_sensitive,external_embedding_allowed FROM knowledge_documents
            WHERE id=:id AND tenant_id=:tenant AND deleted_at IS NULL
              AND (user_id=:user OR visibility='tenant')"""),
            dict(id=document_id, tenant=tenant_id, user=user_id))
        row = result.mappings().first()
        return dict(row) if row else None

    async def set_visibility(self, session, *, tenant_id, user_id, document_id, visibility):
        if visibility not in {"private", "tenant"}:
            raise ValueError("未知知识可见范围")
        result = await session.execute(text("""UPDATE knowledge_documents SET visibility=:visibility,updated_at=now()
            WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND deleted_at IS NULL RETURNING id"""),
            dict(id=document_id, tenant=tenant_id, user=user_id, visibility=visibility))
        return result.first() is not None
