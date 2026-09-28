"""会话数据访问层。"""

from datetime import datetime
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class Conversation:
    """会话表中创建后需要返回给调用方的字段。"""

    id: UUID
    title: str
    status: str
    created_at: datetime


class ConversationRepository:
    """封装 conversations 表的数据库读写。"""

    async def create(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        title: str,
    ) -> Conversation:
        """在当前租户事务内创建归属当前用户的会话。"""
        result = await session.execute(
            text(
                "INSERT INTO conversations (tenant_id, user_id, title) "
                "VALUES (:tenant_id, :user_id, :title) "
                "RETURNING id, title, status, created_at"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "title": title},
        )
        row = result.mappings().one()
        return Conversation(
            id=row["id"],
            title=row["title"],
            status=row["status"],
            created_at=row["created_at"],
        )
