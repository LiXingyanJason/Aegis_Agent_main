"""面向 Aegis API 的任务运行查询仓储。"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.event.run_event import RunEvent


class AgentRunRepository:
    """仅提供页面查询与 SSE 断线补发所需的任务读取能力。

    任务领取、模型回复写入、步骤状态更新和工具调用审计属于独立
    ``aegis_agent_worker`` 服务的持久化职责，不能放入 API 服务。
    """

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
