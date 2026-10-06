"""Agent 任务运行、步骤和 Agent 回复的数据库访问。"""

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aegis_agent_worker.event.run_event import RunEvent


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    """Worker 成功领取 queued 任务后需要的归属信息。"""

    id: UUID
    tenant_id: UUID
    user_id: UUID
    conversation_id: UUID


class AgentRunRepository:
    """封装 Agent Worker 对 agent_runs、run_steps、消息表的最小读写。"""

    async def claim_queued_run(
        self,
        session: AsyncSession,
        run_id: UUID,
        *,
        model_provider: str,
        model_name: str,
    ) -> ClaimedRun | None:
        """原子领取 queued 任务；已被其他 Worker 领取时返回 None。"""
        result = await session.execute(
            text(
                "UPDATE agent_runs "
                "SET status = 'running', current_stage = '正在调用模型', "
                "    model_provider = :model_provider, model_name = :model_name, "
                "    started_at = COALESCE(started_at, now()), updated_at = now() "
                "WHERE id = :run_id AND status = 'queued' "
                "RETURNING id, tenant_id, user_id, conversation_id"
            ),
            {"run_id": run_id, "model_provider": model_provider, "model_name": model_name},
        )
        row = result.mappings().first()
        if row is None:
            return None
        return ClaimedRun(
            id=row["id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            conversation_id=row["conversation_id"],
        )

    async def load_conversation_history(
        self,
        session: AsyncSession,
        *,
        conversation_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        """读取最近 20 条用户/Agent 可见消息，作为模型上下文。"""
        result = await session.execute(
            text(
                "SELECT role, content FROM ("
                "  SELECT cm.role, cm.content, cm.sequence_no "
                "  FROM conversation_messages cm "
                "  JOIN conversations c ON c.id = cm.conversation_id "
                "  WHERE cm.conversation_id = :conversation_id "
                "    AND cm.tenant_id = :tenant_id "
                "    AND c.user_id = :user_id "
                "    AND cm.role IN ('user', 'assistant') "
                "  ORDER BY cm.sequence_no DESC "
                "  LIMIT 20"
                ") recent_messages ORDER BY sequence_no"
            ),
            {
                "conversation_id": conversation_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
            },
        )
        return [dict(row) for row in result.mappings().all()]

    async def create_running_llm_step(
        self, session: AsyncSession, *, run_id: UUID, tenant_id: UUID
    ) -> UUID:
        """记录当前任务正调用模型的可追溯步骤。"""
        result = await session.execute(
            text(
                "INSERT INTO run_steps (tenant_id, run_id, sequence_no, step_key, label, status, started_at) "
                "SELECT :tenant_id, :run_id, COALESCE(MAX(sequence_no), 0) + 1, "
                "       'llm_generate', '正在生成回复', 'running', now() "
                "FROM run_steps WHERE run_id = :run_id AND tenant_id = :tenant_id "
                "RETURNING id"
            ),
            {"run_id": run_id, "tenant_id": tenant_id},
        )
        step_id = result.mappings().one()["id"]
        await session.execute(
            text(
                "UPDATE agent_runs SET current_stage = '正在生成回复', updated_at = now() "
                "WHERE id = :run_id AND tenant_id = :tenant_id"
            ),
            {"run_id": run_id, "tenant_id": tenant_id},
        )
        return step_id

    async def create_running_tool_step(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        tenant_id: UUID,
        step_key: str,
        label: str,
    ) -> UUID:
        """记录正在执行的只读工具步骤，并将任务阶段更新为该步骤。"""
        result = await session.execute(
            text(
                "INSERT INTO run_steps (tenant_id, run_id, sequence_no, step_key, label, status, started_at) "
                "SELECT :tenant_id, :run_id, COALESCE(MAX(sequence_no), 0) + 1, "
                "       :step_key, :label, 'running', now() "
                "FROM run_steps WHERE run_id = :run_id AND tenant_id = :tenant_id RETURNING id"
            ),
            {"run_id": run_id, "tenant_id": tenant_id, "step_key": step_key, "label": label},
        )
        step_id = result.mappings().one()["id"]
        await session.execute(
            text(
                "UPDATE agent_runs SET current_stage = :label, updated_at = now() "
                "WHERE id = :run_id AND tenant_id = :tenant_id"
            ),
            {"run_id": run_id, "tenant_id": tenant_id, "label": label},
        )
        return step_id

    async def finish_step(
        self,
        session: AsyncSession,
        *,
        step_id: UUID,
        run_id: UUID,
        tenant_id: UUID,
        status: str,
        detail: str,
    ) -> None:
        """结束一个工具步骤；调用者仅传入数据库允许的 succeeded 或 failed 状态。"""
        await session.execute(
            text(
                "UPDATE run_steps SET status = :status, detail = :detail, finished_at = now() "
                "WHERE id = :step_id AND run_id = :run_id AND tenant_id = :tenant_id"
            ),
            {
                "step_id": step_id,
                "run_id": run_id,
                "tenant_id": tenant_id,
                "status": status,
                "detail": detail[:1000],
            },
        )

    async def complete_run_with_assistant_message(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        conversation_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        assistant_content: str,
        step_id: UUID | None,
    ) -> None:
        """保存模型回复，并将步骤和任务统一标记为完成。"""
        content = assistant_content[:65536]
        lock_result = await session.execute(
            text(
                "SELECT id FROM conversations "
                "WHERE id = :conversation_id AND tenant_id = :tenant_id AND user_id = :user_id "
                "FOR UPDATE"
            ),
            {"conversation_id": conversation_id, "tenant_id": tenant_id, "user_id": user_id},
        )
        if lock_result.mappings().first() is None:
            raise RuntimeError("任务关联会话不存在或无权访问")
        sequence_result = await session.execute(
            text(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence_no "
                "FROM conversation_messages "
                "WHERE conversation_id = :conversation_id AND tenant_id = :tenant_id"
            ),
            {"conversation_id": conversation_id, "tenant_id": tenant_id},
        )
        await session.execute(
            text(
                "INSERT INTO conversation_messages "
                "(tenant_id, conversation_id, run_id, role, content, display_content, sequence_no, content_hash) "
                "VALUES (:tenant_id, :conversation_id, :run_id, 'assistant', :content, :display_content, "
                "        :sequence_no, :content_hash)"
            ),
            {
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "run_id": run_id,
                "content": content,
                "display_content": content,
                "sequence_no": sequence_result.mappings().one()["next_sequence_no"],
                "content_hash": sha256(content.encode("utf-8")).hexdigest(),
            },
        )
        if step_id is not None:
            await session.execute(
                text(
                    "UPDATE run_steps SET status = 'succeeded', detail = '模型回复已保存', finished_at = now() "
                    "WHERE id = :step_id AND run_id = :run_id AND tenant_id = :tenant_id"
                ),
                {"step_id": step_id, "run_id": run_id, "tenant_id": tenant_id},
            )
        await session.execute(
            text(
                "UPDATE agent_runs "
                "SET status = 'completed', current_stage = '已完成', result_summary = :result_summary, "
                "    finished_at = now(), updated_at = now() "
                "WHERE id = :run_id AND tenant_id = :tenant_id AND status = 'running'"
            ),
            {"run_id": run_id, "tenant_id": tenant_id, "result_summary": content[:1000]},
        )
        await session.execute(
            text(
                "UPDATE conversations SET last_message_at = now(), updated_at = now() "
                "WHERE id = :conversation_id AND tenant_id = :tenant_id AND user_id = :user_id"
            ),
            {"conversation_id": conversation_id, "tenant_id": tenant_id, "user_id": user_id},
        )

    async def fail_run(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        tenant_id: UUID,
        step_id: UUID | None,
        error_code: str,
        error_message: str,
    ) -> None:
        """记录模型或执行失败，并结束正在运行的步骤。"""
        if step_id is not None:
            await session.execute(
                text(
                    "UPDATE run_steps SET status = 'failed', detail = :detail, finished_at = now() "
                    "WHERE id = :step_id AND run_id = :run_id AND tenant_id = :tenant_id"
                ),
                {
                    "step_id": step_id,
                    "run_id": run_id,
                    "tenant_id": tenant_id,
                    "detail": error_message[:1000],
                },
            )
        await session.execute(
            text(
                "UPDATE agent_runs "
                "SET status = 'failed', current_stage = '执行失败', error_code = :error_code, "
                "    error_message = :error_message, finished_at = now(), updated_at = now() "
                "WHERE id = :run_id AND tenant_id = :tenant_id"
            ),
            {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "error_code": error_code,
                "error_message": error_message[:1000],
            },
        )

    async def find_run_detail_for_user(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any] | None:
        """按当前租户和用户读取一次任务运行，避免泄露其他用户的任务。"""
        result = await session.execute(
            text(
                "SELECT id, conversation_id, status, current_stage, model_provider, model_name, "
                "       result_summary, error_code, error_message, started_at, finished_at, created_at "
                "FROM agent_runs "
                "WHERE id = :run_id AND tenant_id = :tenant_id AND user_id = :user_id"
            ),
            {"run_id": run_id, "tenant_id": tenant_id, "user_id": user_id},
        )
        row = result.mappings().first()
        return dict(row) if row is not None else None

    async def list_steps_for_run_detail(
        self, session: AsyncSession, *, run_id: UUID, tenant_id: UUID
    ) -> list[dict[str, Any]]:
        """读取任务的执行步骤，供前端轮询展示当前进度。"""
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

    async def append_event(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        tenant_id: UUID,
        event_type: str,
        payload: dict[str, Any],
    ) -> RunEvent:
        """为任务追加一条持久事件；调用方在提交后负责实时发布。"""
        result = await session.execute(
            text(
                "INSERT INTO run_events (tenant_id, run_id, event_no, event_type, payload) "
                "SELECT :tenant_id, :run_id, COALESCE(MAX(event_no), 0) + 1, "
                "       :event_type, CAST(:payload AS jsonb) "
                "FROM run_events "
                "WHERE run_id = :run_id AND tenant_id = :tenant_id "
                "RETURNING event_no, event_type, payload, created_at"
            ),
            {
                "tenant_id": tenant_id,
                "run_id": run_id,
                "event_type": event_type,
                "payload": json.dumps(payload, ensure_ascii=False),
            },
        )
        row = result.mappings().one()
        return RunEvent(
            run_id=run_id,
            event_no=row["event_no"],
            event_type=row["event_type"],
            payload=_normalize_json_payload(row["payload"]),
            created_at=row["created_at"],
        )

    async def list_events_for_run(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        tenant_id: UUID,
        after_event_no: int,
    ) -> list[RunEvent]:
        """读取断线恢复所需的事件历史，不返回其他租户的事件。"""
        result = await session.execute(
            text(
                "SELECT event_no, event_type, payload, created_at "
                "FROM run_events "
                "WHERE run_id = :run_id AND tenant_id = :tenant_id "
                "  AND event_no > :after_event_no "
                "ORDER BY event_no"
            ),
            {"run_id": run_id, "tenant_id": tenant_id, "after_event_no": after_event_no},
        )
        return [
            RunEvent(
                run_id=run_id,
                event_no=row["event_no"],
                event_type=row["event_type"],
                payload=_normalize_json_payload(row["payload"]),
                created_at=row["created_at"],
            )
            for row in result.mappings().all()
        ]


def _normalize_json_payload(value: Any) -> dict[str, Any]:
    """兼容 asyncpg 返回 dict 或 JSON 文本两种 payload 形式。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed_value = json.loads(value)
        if isinstance(parsed_value, dict):
            return parsed_value
    raise RuntimeError("run_events.payload 不是 JSON 对象")
