from fastapi import HTTPException
from app.repository.knowledge_repository import KnowledgeRepository


class RerankPermissionService:
    async def change(self,session,user,document_id,allowed):
        if not await KnowledgeRepository().set_rerank_permission(session,user,document_id,allowed):
            raise HTTPException(404,detail='资料不存在或无权操作')
        return dict(document_id=document_id,external_rerank_allowed=allowed)
