"""Agent 使用长期记忆时的数据访问。"""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class MemoryRepository:
    """仅暴露读取非敏感记忆和记录实际使用情况的最小能力。"""

    async def list_active_non_sensitive(
        self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        """读取当前用户未删除、非敏感的候选记忆，绝不查询敏感正文。"""
        result = await session.execute(
            text(
                "SELECT id, content, source, updated_at FROM memory_items "
                "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                "AND deleted_at IS NULL AND is_sensitive=false "
                "ORDER BY updated_at DESC, id DESC LIMIT :limit"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "limit": limit},
        )
        return [dict(row) for row in result.mappings().all()]

    async def record_usage(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
        memory_id: UUID,
        display_summary: str,
    ) -> None:
        """记录本次任务确实注入提示词的记忆，不保存完整提示词。"""
        await session.execute(
            text(
                "INSERT INTO run_memory_usages "
                "(tenant_id, run_id, memory_id, usage_purpose, included_in_prompt, display_summary) "
                "VALUES (:tenant_id,:run_id,:memory_id,'context',true,:display_summary)"
            ),
            {
                "tenant_id": tenant_id,
                "run_id": run_id,
                "memory_id": memory_id,
                "display_summary": display_summary[:1000],
            },
        )
