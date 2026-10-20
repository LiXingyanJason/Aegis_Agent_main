"""从 Redis 队列消费 Agent 任务的独立 Worker 进程。"""

import asyncio
import logging
import sys
import json
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
from aegis_agent_worker.service.runtime.input_request_service import InputRequestService
from time import monotonic

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from aegis_agent_worker.agent.orchestrator import AgentOrchestrator
from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
from aegis_agent_worker.agent.specialists.calendar_agent import CalendarAgent
from aegis_agent_worker.agent.specialists.main_agent import MainAgent
from aegis_agent_worker.service.runtime.decision_context_service import DecisionContextService
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
from aegis_agent_worker.service.calendar.calendar_planning_service import CalendarPlanningService
from aegis_agent_worker.service.mail.mail_send_service import MailSendService
from aegis_agent_worker.service.mail.mail_task_service import MailTaskService
from aegis_agent_worker.service.mail.mail_query_service import MailQueryService
from aegis_agent_worker.service.mail.mail_collection_service import MailCollectionService
from aegis_agent_worker.service.mail.mail_revision_service import MailRevisionService
from aegis_agent_worker.service.runtime.card_context_service import CardContextService
from aegis_agent_worker.service.runtime.main_context_tool_service import MainContextToolService
from aegis_agent_worker.harness.policy_provider import PolicyProvider
from aegis_agent_worker.harness.policy_provider import (LEGACY_POLICY, KNOWLEDGE_CONTRACT_POLICY,
    COOPERATION_POLICY, COOPERATION_KNOWLEDGE_POLICY)
from aegis_agent_worker.agent.workflows.cooperation.factory import CooperationWorkflowFactory
from aegis_agent_worker.agent.specialists.cooperation_planner import CooperationPlanner
from aegis_agent_worker.service.cooperation.cooperation_service import CooperationService
from aegis_agent_worker.agent.specialists.knowledge_agent import KnowledgeAgent
from aegis_agent_worker.agent.workflows.knowledge.factory import KnowledgeWorkflowFactory
from aegis_agent_worker.service.knowledge.evidence_service import EvidenceService
from aegis_agent_worker.repository.knowledge_evidence_repository import KnowledgeEvidenceRepository
from aegis_agent_worker.service.mail.mail_planning_service import MailPlanningService
from aegis_agent_worker.service.memory.memory_context_service import MemoryContextService
from aegis_agent_worker.service.memory.session_memory_service import SessionMemoryService
from aegis_agent_worker.service.runtime.conversation_context_service import ConversationContextService
from aegis_agent_worker.storage.session_memory_cache import RedisSessionMemoryCache
from aegis_agent_worker.service.runtime.conversation_artifact_service import ConversationArtifactService
from aegis_agent_worker.service.calendar.calendar_tool_execution_service import CalendarToolExecutionService
from aegis_agent_worker.service.knowledge.knowledge_query_service import KnowledgeQueryService
from aegis_agent_worker.service.knowledge.workflow_progress_service import KnowledgeWorkflowProgress
from aegis_agent_worker.service.knowledge.hybrid_retrieval_service import HybridRetrievalService
from aegis_agent_worker.repository.knowledge_retrieval_repository import KnowledgeRetrievalRepository
from aegis_agent_worker.embedding.bootstrap import create_embedding_executor
from aegis_agent_worker.vector.qdrant_search import QdrantSearch

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
            default_policy=(COOPERATION_KNOWLEDGE_POLICY if settings.knowledge_query_enabled else COOPERATION_POLICY) if settings.cooperation_enabled else (
                KNOWLEDGE_CONTRACT_POLICY if settings.knowledge_query_enabled else LEGACY_POLICY)
            policies = PolicyProvider((LEGACY_POLICY, KNOWLEDGE_CONTRACT_POLICY,COOPERATION_POLICY,COOPERATION_KNOWLEDGE_POLICY),
                default_version=default_policy.version)
            harness = HarnessExecutor(database, policy_provider=policies)
            llm_client = GuardedLLMClient(OpenAICompatibleClient(settings), harness)
            events = RunEventPublisher(redis)
            runs = AgentRunRepository()
            session_memory = SessionMemoryService(database,
                RedisSessionMemoryCache(redis,policies.current().session_memory),
                policy_provider=policies) if settings.session_memory_enabled else None
            context_service = ConversationContextService(database,runs,session_memory=session_memory,
                policy_provider=policies)
            input_requests=InputRequestService(database,runs,events)
            memory_context = MemoryContextService(database)
            cards = CardContextService(database, policy_provider=policies)
            knowledge_retrieval = HybridRetrievalService(settings,KnowledgeRetrievalRepository(database),
                create_embedding_executor(settings,database),QdrantSearch(settings)) if settings.knowledge_query_enabled else None
            tools = create_default_tool_gateway(settings,harness=harness,card_context=cards,
                knowledge_retrieval=knowledge_retrieval)
            tool_calls = ToolCallRepository()
            tool_execution = CalendarToolExecutionService(database, runs, tools, tool_calls, events)
            artifact_service = ConversationArtifactService(database, runs, events)
            calendar_confirmation = CalendarConfirmationService(
                database, runs, tool_execution, events, tools, tool_calls
            )
            calendar_workflows = CalendarWorkflowFactory(
                calendar_agent=CalendarAgent(settings.app_timezone, llm_client, policy_provider=policies),
                tool_execution=tool_execution,
                confirmation_service=calendar_confirmation,
                artifact_service=artifact_service,
                planning_audit=CalendarPlanningService(database, runs, events),
                memory_context=memory_context,
            )
            mail_tasks = MailTaskService(database, runs, tools, events)
            mail_send = MailSendService(database, runs, tools, events)
            mail_workflows = MailWorkflowFactory(
                mail_tasks=mail_tasks,
                mail_send=mail_send,
                mail_agent=MailAgent(llm_client, settings.app_timezone, policy_provider=policies),
                memory_context=memory_context,
                query_service=MailQueryService(database, runs, tools, events),
                artifact_service=artifact_service,
                planning_audit=MailPlanningService(database, runs, events),
                collection_service=MailCollectionService(database,runs,tools,events),
                input_requests=input_requests,
                revision_service=MailRevisionService(database,runs,events),
            )
            knowledge_workflows = KnowledgeWorkflowFactory(
                agent=KnowledgeAgent(llm_client, policy_provider=policies),
                query_service=KnowledgeQueryService(database, runs, tools, tool_calls, events),
                evidence_service=EvidenceService(KnowledgeEvidenceRepository(database)), artifact_service=artifact_service,
                enabled=settings.knowledge_query_enabled,
                progress_service=KnowledgeWorkflowProgress(database,runs,events),
            )
            workflow_factories=(calendar_workflows,mail_workflows,knowledge_workflows)
            if settings.cooperation_enabled:
                workflow_factories=(*workflow_factories,CooperationWorkflowFactory(
                    mail_workflows.mail_agent,mail_workflows.query_service,calendar_workflows.calendar_agent,
                    calendar_confirmation,CooperationService(database,runs,tools,events),
                    CooperationPlanner(llm_client,policy_provider=policies),memory_context))
            orchestrator = AgentOrchestrator(
                database,
                llm_client,
                runs=runs,
                context_service=context_service,
                events=events,
                workflow_factories=workflow_factories,
                checkpointer=checkpointer,
                mail_tasks=mail_tasks,
                memory_context=memory_context,
                main_agent=MainAgent(llm_client, settings.app_timezone, policy_provider=policies),
                decision_context_service=DecisionContextService(database, runs, events),
                harness=harness,
                input_requests=input_requests,
                main_context_tools=MainContextToolService(database, runs, tools, cards, events),
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
                            run_id, resume_decision=queued_message.resume_decision,
                            resume_input_id=queued_message.resume_input_id,
                        )
                    if result.status == "failed":
                        await redis.rpush(settings.agent_queue_name+":dead-letter",
                                          json.dumps({"run_id":str(run_id),"error_code":result.error_code}))
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
