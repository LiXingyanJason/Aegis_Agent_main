"""会话数据访问层。"""

from datetime import datetime
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.structured_logging import get_current_trace_id


@dataclass(frozen=True, slots=True)
class Conversation:
    """会话表中创建后需要返回给调用方的字段。"""

    id: UUID
    title: str
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SentMessageRun:
    """发送消息后返回的消息、任务运行及是否为幂等重放结果。"""

    message_id: UUID
    run_id: UUID
    status: str
    is_replay: bool


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

    async def list_for_user(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        """返回当前用户可恢复的会话摘要，按最近活动时间倒序排列。"""
        result = await session.execute(
            text(
                "SELECT c.id, c.title, c.status, c.last_message_at, c.created_at, "
                "(SELECT cm.display_content "
                " FROM conversation_messages cm "
                " WHERE cm.conversation_id = c.id "
                " ORDER BY cm.sequence_no DESC "
                " LIMIT 1) AS latest_message_preview "
                "FROM conversations c "
                "WHERE c.tenant_id = :tenant_id AND c.user_id = :user_id "
                "ORDER BY c.last_message_at DESC NULLS LAST, c.created_at DESC"
            ),
            {"tenant_id": tenant_id, "user_id": user_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def find_detail_for_user(
        self,
        session: AsyncSession,
        *,
        conversation_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any] | None:
        """按租户和用户读取一个会话，避免暴露其他用户的会话是否存在。"""
        result = await session.execute(
            text(
                "SELECT id, title, status, last_message_at, created_at, updated_at "
                "FROM conversations "
                "WHERE id = :conversation_id "
                "  AND tenant_id = :tenant_id "
                "  AND user_id = :user_id"
            ),
            {
                "conversation_id": conversation_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
            },
        )
        row = result.mappings().first()
        return dict(row) if row is not None else None

    async def list_messages_for_conversation(
        self,
        session: AsyncSession,
        *,
        conversation_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        """读取页面可展示的消息内容，不返回内部原始提示词等敏感执行数据。"""
        result = await session.execute(
            text(
                "SELECT cm.id, cm.role, cm.display_content, cm.sequence_no, cm.is_final, "
                "       cm.run_id, cm.tool_call_id, cm.created_at "
                "FROM conversation_messages cm "
                "JOIN conversations c ON c.id = cm.conversation_id "
                "WHERE cm.conversation_id = :conversation_id "
                "  AND cm.tenant_id = :tenant_id "
                "  AND c.user_id = :user_id "
                "ORDER BY cm.sequence_no"
            ),
            {
                "conversation_id": conversation_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
            },
        )
        return [dict(row) for row in result.mappings().all()]

    async def list_runs_for_conversation(
        self,
        session: AsyncSession,
        *,
        conversation_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        """读取会话内任务运行记录，用于恢复右侧执行进度。"""
        result = await session.execute(
            text(
                "SELECT id, status, current_stage, result_summary, error_code, error_message, "
                "       started_at, finished_at, created_at "
                "FROM agent_runs "
                "WHERE conversation_id = :conversation_id "
                "  AND tenant_id = :tenant_id "
                "  AND user_id = :user_id "
                "ORDER BY created_at"
            ),
            {
                "conversation_id": conversation_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
            },
        )
        return [dict(row) for row in result.mappings().all()]

    async def list_steps_for_run(
        self, session: AsyncSession, *, run_id: UUID, tenant_id: UUID
    ) -> list[dict[str, Any]]:
        """读取一个任务的阶段进度。"""
        result = await session.execute(
            text(
                "SELECT id, sequence_no, step_key, label, status, detail, started_at, finished_at "
                "FROM run_steps "
                "WHERE run_id = :run_id AND tenant_id = :tenant_id "
                "ORDER BY sequence_no"
            ),
            {"run_id": run_id, "tenant_id": tenant_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def list_tool_previews_for_run(
        self, session: AsyncSession, *, run_id: UUID, tenant_id: UUID
    ) -> list[dict[str, Any]]:
        """读取工具调用的安全预览；不向前端暴露完整请求或响应载荷。"""
        result = await session.execute(
            text(
                "SELECT id, tool_name, risk_level, input_summary, output_summary, status, "
                "       error_code, error_message, duration_ms, created_at "
                "FROM tool_calls "
                "WHERE run_id = :run_id AND tenant_id = :tenant_id "
                "ORDER BY created_at"
            ),
            {"run_id": run_id, "tenant_id": tenant_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def list_approval_items_for_run(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        """读取当前用户仍可查看的逐项外部操作确认信息。"""
        result = await session.execute(
            text(
                "SELECT id, action, risk_level, resource_type, resource_id, title, "
                "       preview_snapshot, draft_version, status, expires_at, created_at "
                "FROM approval_items "
                "WHERE run_id = :run_id "
                "  AND tenant_id = :tenant_id "
                "  AND user_id = :user_id "
                "ORDER BY created_at"
            ),
            {"run_id": run_id, "tenant_id": tenant_id, "user_id": user_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def create_message_and_run(
        self,
        session: AsyncSession,
        *,
        conversation_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        content: str,
        client_message_id: str,
    ) -> SentMessageRun | None:
        """原子地保存用户消息并创建 queued 任务；重复消息标识返回既有任务。"""
        # 锁定会话行，以串行化同一会话的 sequence_no 分配及相同消息标识的并发重试。
        owned_result = await session.execute(
            text(
                "SELECT id FROM conversations "
                "WHERE id = :conversation_id "
                "  AND tenant_id = :tenant_id "
                "  AND user_id = :user_id "
                "FOR UPDATE"
            ),
            {
                "conversation_id": conversation_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
            },
        )
        if owned_result.mappings().first() is None:
            return None

        # 查询位于行锁之后：并发的重复请求在此可见首个事务已创建的消息和任务。
        existing_result = await session.execute(
            text(
                "SELECT cm.id AS message_id, cm.run_id, ar.status "
                "FROM conversation_messages cm "
                "LEFT JOIN agent_runs ar ON ar.id = cm.run_id "
                "WHERE cm.conversation_id = :conversation_id "
                "  AND cm.client_message_id = :client_message_id "
                "  AND cm.tenant_id = :tenant_id"
            ),
            {
                "conversation_id": conversation_id,
                "client_message_id": client_message_id,
                "tenant_id": tenant_id,
            },
        )
        existing = existing_result.mappings().first()
        if existing is not None:
            # 同一事务创建的消息一定会补齐 run_id；保留该检查以避免返回不完整数据。
            if existing["run_id"] is None or existing["status"] is None:
                raise RuntimeError("重复消息缺少关联任务运行")
            return SentMessageRun(
                message_id=existing["message_id"],
                run_id=existing["run_id"],
                status=existing["status"],
                is_replay=True,
            )

        sequence_result = await session.execute(
            text(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence_no "
                "FROM conversation_messages "
                "WHERE conversation_id = :conversation_id AND tenant_id = :tenant_id"
            ),
            {"conversation_id": conversation_id, "tenant_id": tenant_id},
        )
        sequence_no = sequence_result.mappings().one()["next_sequence_no"]
        content_hash = sha256(content.encode("utf-8")).hexdigest()
        message_result = await session.execute(
            text(
                "INSERT INTO conversation_messages "
                "(tenant_id, conversation_id, role, content, display_content, sequence_no, "
                " client_message_id, content_hash) "
                "VALUES (:tenant_id, :conversation_id, 'user', :content, :display_content, "
                "        :sequence_no, :client_message_id, :content_hash) "
                "RETURNING id"
            ),
            {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "content": content,
                "display_content": content,
                "sequence_no": sequence_no,
                "client_message_id": client_message_id,
                "content_hash": content_hash,
            },
        )
        message_id = message_result.mappings().one()["id"]
        request_id = uuid4().hex
        trace_id = get_current_trace_id()
        run_result = await session.execute(
            text(
                "INSERT INTO agent_runs "
                "(tenant_id, user_id, conversation_id, input_message_id, run_type, status, request_id, trace_id) "
                "VALUES (:tenant_id, :user_id, :conversation_id, :input_message_id, "
                "        'conversation', 'queued', :request_id, :trace_id) "
                "RETURNING id, status"
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "input_message_id": message_id,
                "request_id": request_id,
                "trace_id": trace_id,
            },
        )
        run = run_result.mappings().one()
        await session.execute(
            text(
                "UPDATE conversation_messages SET run_id = :run_id "
                "WHERE id = :message_id AND tenant_id = :tenant_id"
            ),
            {"run_id": run["id"], "message_id": message_id, "tenant_id": tenant_id},
        )
        await session.execute(
            text(
                "UPDATE conversations "
                "SET last_message_at = now(), updated_at = now() "
                "WHERE id = :conversation_id AND tenant_id = :tenant_id AND user_id = :user_id"
            ),
            {"conversation_id": conversation_id, "tenant_id": tenant_id, "user_id": user_id},
        )
        return SentMessageRun(
            message_id=message_id,
            run_id=run["id"],
            status=run["status"],
            is_replay=False,
        )
