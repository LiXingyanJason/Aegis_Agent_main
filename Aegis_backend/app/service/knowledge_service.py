"""知识可见范围的代码授权；管理 HTTP 接口留待导入阶段接入。"""
from app.repository.knowledge_repository import KnowledgeRepository


class KnowledgeService:
    def __init__(self, repository=None):
        self._repository = repository or KnowledgeRepository()

    async def set_visibility(self, session, current_user, document_id, visibility):
        """仅已认证的文档所有者可分享／收回；调用方负责租户事务。"""
        return await self._repository.set_visibility(session, tenant_id=current_user.tenant_id,
            user_id=current_user.id, document_id=document_id, visibility=visibility)
