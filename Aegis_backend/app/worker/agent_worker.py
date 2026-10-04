"""从 Redis 队列消费 Agent 任务的独立 Worker 进程。"""

import asyncio
import logging
from time import monotonic
from uuid import UUID

from app.agent.orchestrator import AgentOrchestrator
from app.agent.tool_router import AgentToolRouter
from app.config.database import Database
from app.config.redis import create_redis_client
from app.config.settings import get_settings
from app.event.run_event import RunEventPublisher
from app.llm.client import OpenAICompatibleClient
from app.tool.bootstrap import create_default_tool_gateway

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    """持续阻塞读取 Redis 队列；单个任务失败不会中断后续任务消费。"""
    settings = get_settings()
    database = Database(settings)
    redis = create_redis_client(settings)
    orchestrator = AgentOrchestrator(
        database,
        OpenAICompatibleClient(settings),
        events=RunEventPublisher(redis),
        tools=create_default_tool_gateway(settings),
        tool_router=AgentToolRouter(settings.app_timezone),
    )
    print(
        f"[Agent Worker] 已启动，正在等待 Redis 队列「{settings.agent_queue_name}」中的任务。",
        flush=True,
    )
    try:
        while True:
            item = await redis.blpop(
                settings.agent_queue_name,
                timeout=settings.agent_worker_poll_timeout_seconds,
            )
            if item is None:
                continue
            _, raw_run_id = item
            print(f"[Agent Worker] 收到任务：run_id={raw_run_id}", flush=True)
            started_at = monotonic()
            try:
                result = await orchestrator.execute(UUID(raw_run_id))
                elapsed_seconds = monotonic() - started_at
                failure_detail = (
                    f"，原因={result.error_code}: {result.error_message}"
                    if result.error_code is not None
                    else ""
                )
                print(
                    f"[Agent Worker] 任务结束：run_id={raw_run_id}，"
                    f"结果={result.status}{failure_detail}，耗时={elapsed_seconds:.2f} 秒。",
                    flush=True,
                )
            except ValueError:
                print(f"[Agent Worker] 忽略无效任务标识：run_id={raw_run_id}", flush=True)
                logger.warning("忽略格式无效的 Agent run_id：%s", raw_run_id)
            except Exception:
                print(f"[Agent Worker] 任务发生未处理异常：run_id={raw_run_id}。", flush=True)
                logger.exception("Agent Worker 执行任务失败：run_id=%s", raw_run_id)
    finally:
        await redis.aclose()
        await database.dispose()
        print("[Agent Worker] 已停止，Redis 与数据库连接已关闭。", flush=True)


def main() -> None:
    """供 `python -m app.worker.agent_worker` 调用的同步入口。"""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
