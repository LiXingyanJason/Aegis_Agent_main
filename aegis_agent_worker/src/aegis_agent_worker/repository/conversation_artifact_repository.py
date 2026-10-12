"""对话结果引用的数据访问。"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aegis_agent_worker.agent.contracts.common import ConversationArtifact
from aegis_agent_worker.repository.run_repository import ClaimedRun


class ConversationArtifactRepository:
    """保存 Worker 产生的短期对话展示引用。"""

    async def upsert_for_run(
        self,
        session: AsyncSession,
        *,
        run: ClaimedRun,
        artifacts: list[ConversationArtifact],
    ) -> list[UUID]:
        """按任务和类型幂等保存 Artifact；同一重试不会重复插入卡片。"""
        if run.conversation_id is None:
            return []
        identifiers: list[UUID] = []
        for artifact in artifacts:
            result = await session.execute(
                text(
                    "INSERT INTO conversation_message_artifacts "
                    "(tenant_id, user_id, conversation_id, run_id, artifact_type, payload, expires_at) "
                    "VALUES (:tenant_id, :user_id, :conversation_id, :run_id, :artifact_type, "
                    "        CAST(:payload AS jsonb), now() + interval '7 days') "
                    "ON CONFLICT (run_id, artifact_type) DO UPDATE "
                    "SET payload = EXCLUDED.payload, expires_at = EXCLUDED.expires_at "
                    "RETURNING id"
                ),
                {
                    "tenant_id": run.tenant_id,
                    "user_id": run.user_id,
                    "conversation_id": run.conversation_id,
                    "run_id": run.id,
                    "artifact_type": artifact.artifact_type,
                    "payload": json.dumps(artifact.payload, ensure_ascii=False),
                },
            )
            identifiers.append(result.mappings().one()["id"])
        return identifiers


def normalize_artifact_payload(value: Any) -> dict[str, Any]:
    """兼容 asyncpg 返回 JSON 对象或 JSON 文本。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError("对话 Artifact payload 不是 JSON 对象")
