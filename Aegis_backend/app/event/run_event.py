"""任务运行事件的内部模型、Redis 通道和发布器。"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from app.config.database import register_after_commit

logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class RunEvent:
    """已持久化且可安全推送到前端的一条任务事件。"""

    run_id: UUID
    event_no: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime

    def to_wire_payload(self) -> str:
        """序列化为 Redis Pub/Sub 与 SSE 共用的 JSON 文本。"""
        return json.dumps(
            {
                "run_id": str(self.run_id),
                "event_no": self.event_no,
                "event_type": self.event_type,
                "payload": self.payload,
                "created_at": self.created_at.isoformat(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_wire_payload(cls, raw_payload: str) -> "RunEvent":
        """将 Redis Pub/Sub 收到的 JSON 文本还原为运行事件。"""
        data = json.loads(raw_payload)
        return cls(
            run_id=UUID(data["run_id"]),
            event_no=int(data["event_no"]),
            event_type=str(data["event_type"]),
            payload=dict(data["payload"]),
            created_at=datetime.fromisoformat(data["created_at"]),
        )


def run_event_channel(run_id: UUID) -> str:
    """返回单个任务专用的 Redis Pub/Sub 通道名称。"""
    return f"aegis:run:{run_id}:events"


class RunEventPublisher:
    """在事务提交后将已落库事件发布给 SSE API 实例。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, event: RunEvent) -> None:
        """立即发布已提交的事件；调用者必须确保数据库事务已成功提交。"""
        await self._redis.publish(run_event_channel(event.run_id), event.to_wire_payload())

    async def publish_best_effort(self, event: RunEvent) -> None:
        """尽力实时发布；发布失败时保留已落库事件供客户端后续补发。"""
        try:
            await self.publish(event)
        except Exception:
            # Redis Pub/Sub 仅承担低延迟通知。事件已写入 PostgreSQL，不能因通知暂时失败而中断任务。
            logger.exception("任务事件实时发布失败，将由 SSE 历史补发恢复：run_id=%s", event.run_id)

    def publish_after_commit(self, event: RunEvent) -> None:
        """登记提交后发布，防止 SSE 接收到尚未提交的数据库状态。"""

        async def publish_event() -> None:
            await self.publish_best_effort(event)

        register_after_commit(publish_event)
