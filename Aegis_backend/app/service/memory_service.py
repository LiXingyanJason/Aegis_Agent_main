"""用户手动长期记忆的应用服务。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.memory_repository import MemoryRepository
from app.repository.user_repository import LocalUser
from app.repository.memory_lifecycle_repository import MemoryLifecycleRepository


class MemoryService:
    """一期不调用 LLM，仅管理用户自己保存或确认的文本偏好。"""

    def __init__(self, repository: MemoryRepository | None = None, lifecycle=None) -> None:
        self._repository = repository or MemoryRepository()
        self._lifecycle=lifecycle or MemoryLifecycleRepository()

    async def revoke_memory(self,session,user,memory_id,param):
        await self._lifecycle.revoke(session,user,memory_id,param.expected_version)
        return await self.get_memory(session,user,memory_id)

    async def list_memories(self, session: AsyncSession, user: LocalUser, query: str | None, cursor: UUID | None, limit: int) -> list[dict]:
        return await self._repository.list_memories(session, tenant_id=user.tenant_id, user_id=user.id, query=query, cursor=cursor, limit=limit)

    async def get_memory(self, session: AsyncSession, user: LocalUser, memory_id: UUID) -> dict | None:
        return await self._repository.find_memory(session, tenant_id=user.tenant_id, user_id=user.id, memory_id=memory_id)

    async def create_memory(self, session: AsyncSession, user: LocalUser, param) -> dict:
        identifier=await self._lifecycle.save_draft(session,user,param)
        return await self.get_memory(session,user,identifier)

    async def confirm_memory(self, session: AsyncSession, user: LocalUser, param) -> dict | None:
        from app.param.memory_confirm_param import MemoryConfirmExistingParam
        if isinstance(param,MemoryConfirmExistingParam):
            identifier=param.memory_id
            version=param.expected_version
        else:
            # 旧确认入口仍是用户显式行为；与创建草稿同一套校验，不伪造模型来源。
            identifier=await self._lifecycle.save_draft(session,user,param)
            version=1
        await self._lifecycle.confirm(session,user,identifier,version,param.replace_memory_id,param.replace_version)
        return await self.get_memory(session,user,identifier)

    async def update_memory(self, session: AsyncSession, user: LocalUser, memory_id: UUID, param) -> dict | None:
        identifier=await self._lifecycle.save_draft(session,user,param,memory_id=memory_id)
        return await self.get_memory(session,user,identifier)

    async def request_deletion(self, session: AsyncSession, user: LocalUser, memory_id: UUID):
        await self._lifecycle.lock(session,user)
        return await self._repository.create_deletion_request(session, tenant_id=user.tenant_id, user_id=user.id, memory_id=memory_id)

    async def delete_memory(self, session: AsyncSession, user: LocalUser, memory_id: UUID, deletion_token: str) -> str:
        await self._lifecycle.lock(session,user)
        return await self._repository.delete_memory(session, tenant_id=user.tenant_id, user_id=user.id, memory_id=memory_id, deletion_token=deletion_token)
