"""用户手动长期记忆的应用服务。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.memory_repository import MemoryRepository
from app.repository.user_repository import LocalUser


class MemoryService:
    """一期不调用 LLM，仅管理用户自己保存或确认的文本偏好。"""

    def __init__(self, repository: MemoryRepository | None = None) -> None:
        self._repository = repository or MemoryRepository()

    async def list_memories(self, session: AsyncSession, user: LocalUser, query: str | None, cursor: UUID | None, limit: int) -> list[dict]:
        return await self._repository.list_memories(session, tenant_id=user.tenant_id, user_id=user.id, query=query, cursor=cursor, limit=limit)

    async def get_memory(self, session: AsyncSession, user: LocalUser, memory_id: UUID) -> dict | None:
        return await self._repository.find_memory(session, tenant_id=user.tenant_id, user_id=user.id, memory_id=memory_id)

    async def create_memory(self, session: AsyncSession, user: LocalUser, param) -> dict:
        return await self._repository.create_memory(session, tenant_id=user.tenant_id, user_id=user.id, content=param.content, is_sensitive=param.is_sensitive, source="user_input")

    async def confirm_memory(self, session: AsyncSession, user: LocalUser, param) -> dict | None:
        return await self._repository.create_memory(session, tenant_id=user.tenant_id, user_id=user.id, content=param.content, is_sensitive=param.is_sensitive, source="user_confirmed", source_run_id=param.source_run_id)

    async def update_memory(self, session: AsyncSession, user: LocalUser, memory_id: UUID, param) -> dict | None:
        return await self._repository.update_memory(session, tenant_id=user.tenant_id, user_id=user.id, memory_id=memory_id, content=param.content, is_sensitive=param.is_sensitive)

    async def request_deletion(self, session: AsyncSession, user: LocalUser, memory_id: UUID):
        return await self._repository.create_deletion_request(session, tenant_id=user.tenant_id, user_id=user.id, memory_id=memory_id)

    async def delete_memory(self, session: AsyncSession, user: LocalUser, memory_id: UUID, deletion_token: str) -> str:
        return await self._repository.delete_memory(session, tenant_id=user.tenant_id, user_id=user.id, memory_id=memory_id, deletion_token=deletion_token)
