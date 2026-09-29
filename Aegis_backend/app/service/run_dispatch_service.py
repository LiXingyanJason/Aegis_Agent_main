"""Agent 任务入队服务。"""

from uuid import UUID

from fastapi import Request
from redis.asyncio import Redis

from app.config.database import register_after_commit
from app.config.settings import Settings


class RunDispatchService:
    """将已提交的 Agent 任务标识投递到 Redis 列表队列。"""

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._queue_name = settings.agent_queue_name

    def enqueue_after_commit(self, run_id: UUID) -> None:
        """登记入队动作，确保 Worker 不会读到尚未提交的数据库任务。"""

        async def enqueue() -> None:
            await self._redis.rpush(self._queue_name, str(run_id))

        register_after_commit(enqueue)


def get_run_dispatcher_from_request(request: Request) -> RunDispatchService:
    """从当前 FastAPI 应用的共享 Redis 与配置中创建入队服务。"""
    return RunDispatchService(request.app.state.redis, request.app.state.settings)
