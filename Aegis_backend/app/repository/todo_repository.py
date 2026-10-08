"""Aegis 站内待办计划的数据访问层。"""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class TodoRepository:
    """封装 todo_drafts 的用户范围查询和编辑。"""

    _select_columns = (
        "SELECT td.id, td.email_id, td.extraction_id, td.content, td.due_at, td.status, "
        "td.completed_at, td.created_at, td.updated_at, em.subject AS source_subject, "
        "COALESCE(em.sender_name, em.sender_email) AS source_sender "
        "FROM todo_drafts td JOIN email_messages em ON em.id = td.email_id "
    )

    async def list_todos(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        status_filter: str | None,
    ) -> list[dict[str, Any]]:
        """列出当前用户待办；默认隐藏已经丢弃的项目。"""
        where = "WHERE td.tenant_id = :tenant_id AND td.user_id = :user_id "
        params: dict[str, Any] = {"tenant_id": tenant_id, "user_id": user_id}
        if status_filter is None:
            where += "AND td.status IN ('draft', 'completed') "
        else:
            where += "AND td.status = :status "
            params["status"] = status_filter
        result = await session.execute(
            text(self._select_columns + where + "ORDER BY td.due_at NULLS LAST, td.updated_at DESC"),
            params,
        )
        return [dict(row) for row in result.mappings().all()]

    async def find_todo(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        todo_id: UUID,
    ) -> dict[str, Any] | None:
        """读取一项属于当前用户的待办及其邮件来源摘要。"""
        result = await session.execute(
            text(
                self._select_columns
                + "WHERE td.id = :todo_id AND td.tenant_id = :tenant_id AND td.user_id = :user_id"
            ),
            {"todo_id": todo_id, "tenant_id": tenant_id, "user_id": user_id},
        )
        row = result.mappings().first()
        return dict(row) if row is not None else None

    async def update_todo(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        todo_id: UUID,
        changes: dict[str, Any],
    ) -> dict[str, Any] | None:
        """更新当前用户的一项待办；完成时间由状态转换在服务端维护。"""
        assignments: list[str] = []
        params: dict[str, Any] = {
            "todo_id": todo_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
        }
        if "content" in changes:
            assignments.append("content = :content")
            params["content"] = changes["content"]
        if "due_at" in changes:
            assignments.append("due_at = :due_at")
            params["due_at"] = changes["due_at"]
        if "status" in changes:
            assignments.append("status = :status")
            params["status"] = changes["status"]
            if changes["status"] == "completed":
                assignments.append("completed_at = COALESCE(completed_at, now())")
            elif changes["status"] == "draft":
                assignments.append("completed_at = NULL")
        assignments.append("updated_at = now()")
        result = await session.execute(
            text(
                "UPDATE todo_drafts SET "
                + ", ".join(assignments)
                + " WHERE id = :todo_id AND tenant_id = :tenant_id AND user_id = :user_id "
                + "RETURNING id"
            ),
            params,
        )
        if result.mappings().first() is None:
            return None
        return await self.find_todo(
            session, tenant_id=tenant_id, user_id=user_id, todo_id=todo_id
        )
