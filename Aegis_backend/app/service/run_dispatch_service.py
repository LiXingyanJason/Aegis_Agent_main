"""Agent 任务入队服务。"""

from uuid import UUID

from fastapi import Request
from redis.asyncio import Redis

from app.config.database import register_after_commit
from app.config.settings import Settings
from app.observability.trace_context import serialize_run_message


class RunDispatchService:
    """
        接口调用 enqueue_after_commit(run_id)
        只登记 enqueue 函数
        继续写 conversation_messages、agent_runs 等数据
        PostgreSQL 事务提交成功
        tenant_transaction 依次执行已登记的回调
        await enqueue()
        Redis RPUSH run_id
        Worker 收到任务
    """

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._queue_name = settings.agent_queue_name

    def enqueue_after_commit(self, run_id: UUID) -> None:
        """登记入队动作，确保 Worker 不会读到尚未提交的数据库任务。"""

        async def enqueue() -> None:
            # Redis 负载同时携带 traceparent，使独立 Worker 的子 Span 能关联到本次 HTTP 请求。
            await self._redis.rpush(self._queue_name, serialize_run_message(run_id))

        register_after_commit(enqueue) # 写入数据库回调函数，等待数据库提交完毕再执行这些函数


def get_run_dispatcher_from_request(request: Request) -> RunDispatchService:
    """从当前 FastAPI 应用的共享 Redis 与配置中创建入队服务。"""
    return RunDispatchService(request.app.state.redis, request.app.state.settings)
