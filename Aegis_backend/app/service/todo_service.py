"""Aegis 站内待办计划的应用服务。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.todo_repository import TodoRepository
from app.repository.user_repository import LocalUser


class TodoService:
    """协调当前用户范围内的待办查询与修改。"""

    def __init__(self, repository: TodoRepository | None = None) -> None:
        self._repository = repository or TodoRepository()

    async def list_todos(self, session: AsyncSession, local_user: LocalUser, status_filter: str | None):
        """返回当前用户的待办计划，默认不展示丢弃项。"""
        return await self._repository.list_todos(
            session,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
            status_filter=status_filter,
        )

    async def get_todo(self, session: AsyncSession, local_user: LocalUser, todo_id):
        """读取一项可展示、可编辑的站内待办。"""
        return await self._repository.find_todo(
            session,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
            todo_id=todo_id,
        )

    async def update_todo(self, session: AsyncSession, local_user: LocalUser, todo_id, param):
        """仅按请求实际提供的字段更新待办。"""
        return await self._repository.update_todo(
            session,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
            todo_id=todo_id,
            changes=param.model_dump(exclude_unset=True),
        )
