"""从 Redis 队列消费 Agent 任务的独立 Worker 进程。"""

import asyncio
import logging
from time import monotonic

from aegis_agent_worker.agent.orchestrator import AgentOrchestrator
from aegis_agent_worker.agent.tool_router import AgentToolRouter
from aegis_agent_worker.config.database import Database
from aegis_agent_worker.config.redis import create_redis_client
from aegis_agent_worker.config.settings import get_settings
from aegis_agent_worker.event.run_event import RunEventPublisher
from aegis_agent_worker.llm.client import OpenAICompatibleClient
from aegis_agent_worker.observability.structured_logging import configure_structured_logging, log_event
from aegis_agent_worker.observability.telemetry import configure_telemetry, get_tracer, instrument_dependencies
from aegis_agent_worker.observability.trace_context import deserialize_run_message
from aegis_agent_worker.tool.bootstrap import create_default_tool_gateway

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    """持续阻塞读取 Redis 队列；单个任务失败不会中断后续任务消费。"""
    settings = get_settings()
    configure_structured_logging()
    configure_telemetry(settings, service_name=settings.otel_service_name)
    database = Database(settings)
    instrument_dependencies(database)
    redis = create_redis_client(settings)
    orchestrator = AgentOrchestrator(
        database,
        OpenAICompatibleClient(settings),
        events=RunEventPublisher(redis),
        tools=create_default_tool_gateway(settings), # 创建 CalendarMCPToolHandler实例
        tool_router=AgentToolRouter(settings.app_timezone),
    )
    log_event(
        logger,
        logging.INFO,
        "agent_worker_started",
        queue_name=settings.agent_queue_name,
    )
    try:
        while True:
            item = await redis.blpop(
                settings.agent_queue_name,
                timeout=settings.agent_worker_poll_timeout_seconds,
            )
            if item is None:
                continue
            _, raw_message = item
            try:
                queued_message = deserialize_run_message(raw_message)
            except ValueError:
                log_event(logger, logging.WARNING, "agent_queue_message_invalid")
                logger.warning("忽略格式无效的 Agent 队列消息")
                continue
            run_id = queued_message.run_id
            log_event(logger, logging.INFO, "agent_run_received", run_id=run_id)
            started_at = monotonic()
            try:
                tracer = get_tracer(__name__)
                with tracer.start_as_current_span("agent.consume", context=queued_message.context) as span:
                    span.set_attribute("aegis.run_id", str(run_id))
                    result = await orchestrator.execute(run_id)
                    elapsed_seconds = monotonic() - started_at
                    log_event(
                        logger,
                        logging.INFO,
                        "agent_run_finished",
                        run_id=run_id,
                        status=result.status,
                        error_code=result.error_code,
                        duration_ms=round(elapsed_seconds * 1000),
                    )
            except Exception:
                log_event(logger, logging.ERROR, "agent_run_unhandled_error", run_id=run_id)
                logger.exception("Agent Worker 执行任务失败")
    finally:
        await redis.aclose()
        await database.dispose()
        log_event(logger, logging.INFO, "agent_worker_stopped")


def main() -> None:
    """供 `python -m app.worker.agent_worker` 调用的同步入口。"""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
