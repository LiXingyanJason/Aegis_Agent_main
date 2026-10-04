"""任务运行查询的业务逻辑。"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.run_repository import AgentRunRepository
from app.repository.user_repository import LocalUser
from app.event.run_event import RunEvent


class RunService:
    """处理任务状态、进度步骤等面向页面的读取规则。"""

    def __init__(self, runs: AgentRunRepository | None = None) -> None:
        self._runs = runs or AgentRunRepository()

    async def get_run_detail(
        self, session: AsyncSession, local_user: LocalUser, run_id: UUID
    ) -> dict[str, Any] | None:
        """返回当前用户拥有的任务运行及其步骤；不存在或无权访问时返回 None。"""
        run = await self._runs.find_run_detail_for_user(
            session,
            run_id=run_id,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
        )
        if run is None:
            return None
        run["steps"] = await self._runs.list_steps_for_run_detail(
            session, run_id=run_id, tenant_id=local_user.tenant_id
        )
        return run

    async def ensure_run_owned(
        self, session: AsyncSession, local_user: LocalUser, run_id: UUID
    ) -> bool:
        """确认任务归属当前用户，供 SSE 建立连接前进行一次权限校验。"""
        run = await self._runs.find_run_detail_for_user(
            session,
            run_id=run_id,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
        )
        return run is not None

    async def list_run_events(
        self,
        session: AsyncSession,
        local_user: LocalUser,
        run_id: UUID,
        after_event_no: int,
    ) -> list[RunEvent]:
        """读取当前用户任务的持久事件，用于 SSE 首次连接和断线续传。"""
        return await self._runs.list_events_for_run(
            session,
            run_id=run_id,
            tenant_id=local_user.tenant_id,
            after_event_no=after_event_no,
        )
