"""长期记忆及删除确认令牌的数据访问层。"""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class MemoryRepository:
    """所有查询均显式限定租户、用户和未删除状态。"""

    async def list_memories(self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID, query: str | None, cursor: UUID | None, limit: int) -> list[dict[str, Any]]:
        """按更新时间倒序搜索用户的有效记忆，使用最后一项 ID 作为游标。"""
        result = await session.execute(
            text(
                "SELECT id, content, source, is_sensitive, updated_at FROM memory_items "
                "WHERE tenant_id=:tenant_id AND user_id=:user_id AND deleted_at IS NULL "
                "AND (CAST(:query AS varchar) IS NULL OR content ILIKE '%' || CAST(:query AS varchar) || '%') "
                "AND (CAST(:cursor AS uuid) IS NULL OR (updated_at, id) < ("
                " SELECT updated_at, id FROM memory_items WHERE id=CAST(:cursor AS uuid) "
                " AND tenant_id=:tenant_id AND user_id=:user_id)) "
                "ORDER BY updated_at DESC, id DESC LIMIT :limit"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "query": query, "cursor": cursor, "limit": limit},
        )
        return [dict(row) for row in result.mappings().all()]

    async def find_memory(self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID, memory_id: UUID) -> dict[str, Any] | None:
        """读取当前用户的一条尚未删除记忆。"""
        result = await session.execute(
            text("SELECT id, content, source, source_run_id, is_sensitive, created_at, updated_at FROM memory_items WHERE id=:memory_id AND tenant_id=:tenant_id AND user_id=:user_id AND deleted_at IS NULL"),
            {"memory_id": memory_id, "tenant_id": tenant_id, "user_id": user_id},
        )
        row = result.mappings().first()
        return None if row is None else dict(row)

    async def create_memory(self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID, content: str, is_sensitive: bool, source: str, source_run_id: UUID | None = None) -> dict[str, Any] | None:
        """保存手动输入或已确认的偏好；确认来源任务必须属于当前用户。"""
        if source_run_id is not None:
            owned = await session.execute(
                text("SELECT 1 FROM agent_runs WHERE id=:run_id AND tenant_id=:tenant_id AND user_id=:user_id"),
                {"run_id": source_run_id, "tenant_id": tenant_id, "user_id": user_id},
            )
            if owned.mappings().first() is None:
                return None
        result = await session.execute(
            text("INSERT INTO memory_items (tenant_id,user_id,content,source,source_run_id,is_sensitive) VALUES (:tenant_id,:user_id,:content,:source,:source_run_id,:is_sensitive) RETURNING id"),
            {"tenant_id": tenant_id, "user_id": user_id, "content": content, "source": source, "source_run_id": source_run_id, "is_sensitive": is_sensitive},
        )
        return await self.find_memory(session, tenant_id=tenant_id, user_id=user_id, memory_id=result.mappings().one()["id"])

    async def update_memory(self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID, memory_id: UUID, content: str, is_sensitive: bool) -> dict[str, Any] | None:
        """修改记忆并废止旧的删除令牌，避免旧确认删除新版内容。"""
        result = await session.execute(
            text("UPDATE memory_items SET content=:content,is_sensitive=:is_sensitive,updated_at=now() WHERE id=:memory_id AND tenant_id=:tenant_id AND user_id=:user_id AND deleted_at IS NULL RETURNING id"),
            {"memory_id": memory_id, "tenant_id": tenant_id, "user_id": user_id, "content": content, "is_sensitive": is_sensitive},
        )
        if result.mappings().first() is None:
            return None
        await session.execute(text("UPDATE memory_deletion_tokens SET status='revoked' WHERE memory_id=:memory_id AND requested_by=:user_id AND status='active'"), {"memory_id": memory_id, "user_id": user_id})
        return await self.find_memory(session, tenant_id=tenant_id, user_id=user_id, memory_id=memory_id)

    async def create_deletion_request(self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID, memory_id: UUID) -> tuple[str, datetime] | None:
        """为一条有效记忆签发十分钟有效的删除令牌，仅持久化哈希。"""
        if await self.find_memory(session, tenant_id=tenant_id, user_id=user_id, memory_id=memory_id) is None:
            return None
        await session.execute(text("UPDATE memory_deletion_tokens SET status='revoked' WHERE memory_id=:memory_id AND requested_by=:user_id AND status='active'"), {"memory_id": memory_id, "user_id": user_id})
        token = token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await session.execute(
            text("INSERT INTO memory_deletion_tokens (tenant_id,memory_id,requested_by,token_hash,status,expires_at) VALUES (:tenant_id,:memory_id,:user_id,:token_hash,'active',:expires_at)"),
            {"tenant_id": tenant_id, "memory_id": memory_id, "user_id": user_id, "token_hash": sha256(token.encode()).hexdigest(), "expires_at": expires_at},
        )
        return token, expires_at

    async def delete_memory(self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID, memory_id: UUID, deletion_token: str) -> str:
        """验证一次性令牌后软删除；返回 not_found、invalid_token 或 deleted。"""
        token = await session.execute(
            text("SELECT id,status,expires_at FROM memory_deletion_tokens WHERE tenant_id=:tenant_id AND memory_id=:memory_id AND requested_by=:user_id AND token_hash=:token_hash FOR UPDATE"),
            {"tenant_id": tenant_id, "memory_id": memory_id, "user_id": user_id, "token_hash": sha256(deletion_token.encode()).hexdigest()},
        )
        row = token.mappings().first()
        if row is None:
            return "invalid_token"
        if row["status"] != "active" or row["expires_at"] <= datetime.now(timezone.utc):
            return "invalid_token"
        result = await session.execute(text("UPDATE memory_items SET deleted_at=now(),deleted_by=:user_id,updated_at=now() WHERE id=:memory_id AND tenant_id=:tenant_id AND user_id=:user_id AND deleted_at IS NULL RETURNING id"), {"memory_id": memory_id, "tenant_id": tenant_id, "user_id": user_id})
        if result.mappings().first() is None:
            return "not_found"
        await session.execute(text("UPDATE memory_deletion_tokens SET status='used',used_at=now() WHERE id=:token_id"), {"token_id": row["id"]})
        return "deleted"
