"""从 Redis 队列消费 Agent 任务的独立 Worker 进程。"""

import asyncio
import logging
import sys
from time import monotonic

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from aegis_agent_worker.agent.orchestrator import AgentOrchestrator
from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
from aegis_agent_worker.agent.specialists.calendar_agent import CalendarAgent
from aegis_agent_worker.agent.workflows.calendar.factory import CalendarWorkflowFactory
from aegis_agent_worker.agent.workflows.mail.factory import MailWorkflowFactory
from aegis_agent_worker.config.database import Database
from aegis_agent_worker.config.redis import create_redis_client
from aegis_agent_worker.config.settings import get_settings
from aegis_agent_worker.event.run_event import RunEventPublisher
from aegis_agent_worker.llm.client import OpenAICompatibleClient
from aegis_agent_worker.observability.structured_logging import configure_structured_logging, log_event
from aegis_agent_worker.observability.telemetry import configure_telemetry, get_tracer, instrument_dependencies
from aegis_agent_worker.observability.trace_context import deserialize_run_message
from aegis_agent_worker.tool.bootstrap import create_default_tool_gateway
from aegis_agent_worker.repository.run_repository import AgentRunRepository
from aegis_agent_worker.repository.tool_repository import ToolCallRepository
from aegis_agent_worker.service.calendar.calendar_confirmation_service import CalendarConfirmationService
from aegis_agent_worker.service.mail.mail_send_service import MailSendService
from aegis_agent_worker.service.mail.mail_task_service import MailTaskService
from aegis_agent_worker.service.memory.memory_context_service import MemoryContextService
from aegis_agent_worker.service.tool.tool_execution_service import ToolExecutionService

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    """持续读取 Redis；Checkpointer 使同一 run 可跨消费轮次恢复。"""
    settings = get_settings()
    configure_structured_logging()
    configure_telemetry(settings, service_name=settings.otel_service_name)
    database = Database(settings)
    instrument_dependencies(database)
    redis = create_redis_client(settings)
    try:
        # SQLAlchemy 使用 asyncpg URL；LangGraph 的 AsyncPostgresSaver 使用 psycopg URL。
        async with AsyncPostgresSaver.from_conn_string(settings.database_psycopg_url) as checkpointer:
            await checkpointer.setup() # 建立checkpointer数据库表，我们通过run_id关联每一个checkpointer
            llm_client = OpenAICompatibleClient(settings)
            events = RunEventPublisher(redis)
            runs = AgentRunRepository()
            memory_context = MemoryContextService(database)
            tools = create_default_tool_gateway(settings)
            tool_calls = ToolCallRepository()
            tool_execution = ToolExecutionService(database, runs, tools, tool_calls, events)
            calendar_confirmation = CalendarConfirmationService(
                database, runs, tool_execution, events, tools, tool_calls
            )
            calendar_workflows = CalendarWorkflowFactory(
                calendar_agent=CalendarAgent(settings.app_timezone),
                tool_execution=tool_execution,
                confirmation_service=calendar_confirmation,
            )
            mail_tasks = MailTaskService(database, runs, tools, events)
            mail_send = MailSendService(database, runs, tools, events)
            mail_workflows = MailWorkflowFactory(
                mail_tasks=mail_tasks,
                mail_send=mail_send,
                mail_agent=MailAgent(llm_client),
                memory_context=memory_context,
            )
            orchestrator = AgentOrchestrator(
                database,
                llm_client,
                runs=runs,
                events=events,
                workflow_factories=(calendar_workflows, mail_workflows),
                checkpointer=checkpointer,
                mail_tasks=mail_tasks,
                memory_context=memory_context,
            )
            log_event(logger, logging.INFO, "agent_worker_started", queue_name=settings.agent_queue_name)
            while True:
                item = await redis.blpop(settings.agent_queue_name, timeout=settings.agent_worker_poll_timeout_seconds)
                if item is None:
                    continue
                _, raw_message = item
                try:
                    queued_message = deserialize_run_message(raw_message)
                except ValueError:
                    log_event(logger, logging.WARNING, "agent_queue_message_invalid")
                    continue
                run_id = queued_message.run_id
                started_at = monotonic()
                try:
                    with get_tracer(__name__).start_as_current_span("agent.consume", context=queued_message.context):
                        result = await orchestrator.execute(
                            run_id, resume_decision=queued_message.resume_decision
                        )
                    log_event(logger, logging.INFO, "agent_run_finished", run_id=run_id, status=result.status, error_code=result.error_code, duration_ms=round((monotonic() - started_at) * 1000))
                except Exception:
                    log_event(logger, logging.ERROR, "agent_run_unhandled_error", run_id=run_id)
                    logger.exception("Agent Worker 执行任务失败")
    finally:
        await redis.aclose()
        await database.dispose()
        log_event(logger, logging.INFO, "agent_worker_stopped")


def main() -> None:
    """供 `python -m aegis_agent_worker` 调用的同步入口。"""
    if sys.platform == "win32":
        # psycopg 的异步 PostgreSQL 连接不支持 Windows 默认 ProactorEventLoop；
        # 仅为独立 Worker 切换为兼容的 Selector 事件循环策略。
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
