"""最小 Agent 执行编排：选择流程、调用 LLM，不直接处理持久化事务。"""

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from aegis_agent_worker.agent.tool_router import AgentToolRouter
from aegis_agent_worker.service.conversation_context_service import ConversationContextService
from aegis_agent_worker.service.run_lifecycle_service import RunLifecycleService
from aegis_agent_worker.service.tool_execution_service import ToolExecutionService
from aegis_agent_worker.config.database import Database, TenantContext
from aegis_agent_worker.event.run_event import RunEventPublisher
from aegis_agent_worker.llm.client import LLMError, LLMMessage, OpenAICompatibleClient
from aegis_agent_worker.observability.telemetry import get_tracer
from aegis_agent_worker.repository.run_repository import AgentRunRepository
from aegis_agent_worker.repository.tool_repository import ToolCallRepository
from aegis_agent_worker.tool.gateway import ToolGateway

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    """Worker 输出所需的任务执行结果；错误信息已在业务层脱敏。"""

    status: str
    error_code: str | None = None
    error_message: str | None = None


class AgentOrchestrator:
    """消费一个 queued 任务并协调第一版文本回复；持久化由应用服务负责。"""

    _system_prompt = "你是 Aegis PA 个人效率助手。请基于当前会话，给出准确、简洁、可执行的中文回复。"

    def __init__(
        self,
        database: Database,
        llm_client: OpenAICompatibleClient,
        runs: AgentRunRepository | None = None,
        events: RunEventPublisher | None = None,
        tools: ToolGateway | None = None,
        tool_calls: ToolCallRepository | None = None,
        tool_router: AgentToolRouter | None = None,
        lifecycle: RunLifecycleService | None = None,
        context_service: ConversationContextService | None = None,
        tool_execution: ToolExecutionService | None = None,
    ) -> None:
        """保留原有注入参数，并允许测试或后续图节点替换应用服务。"""
        run_repository = runs or AgentRunRepository()
        self._llm_client = llm_client
        self._lifecycle = lifecycle or RunLifecycleService(database, run_repository, events)
        self._context_service = context_service or ConversationContextService(database, run_repository)
        self._tool_execution = tool_execution
        if self._tool_execution is None and tools is not None:
            self._tool_execution = ToolExecutionService(
                database,
                run_repository,
                tools,
                tool_calls or ToolCallRepository(),
                events,
            )
        self._tool_router = tool_router or AgentToolRouter()

    async def execute(self, run_id: UUID) -> AgentExecutionResult:
        """执行任务，并返回 `completed`、`failed` 或 `ignored` 供 Worker 输出运行状态。"""
        run = await self._lifecycle.claim_queued_run(
            run_id,
            model_provider=self._llm_client.provider_name,
            model_name=self._llm_client.model_name,
        )
        if run is None:
            return AgentExecutionResult(status="ignored")

        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        step_id: UUID | None = None
        try:
            history = await self._context_service.load_history(run, context)

            # 根据当前会话最新的用户消息，决定是否调用工具；若调用，则把工具结果整理成给 LLM 使用的可信上下文
            # 当前不是 LLM 选择工具，而是后端代码按关键词写死路由规则
            tool_context = await self._execute_selected_tool(run, context, history)

            step_id = await self._lifecycle.start_llm_generation(run, context)

            # 组装发送给 LLM 的消息列表(系统提示词，可信工具结果，会话历史消息，最新用户问题)
            messages = [LLMMessage(role="system", content=self._system_prompt)]
            if tool_context is not None:
                messages.append(LLMMessage(role="system", content=tool_context))
            messages.extend(LLMMessage(role=item["role"], content=item["content"]) for item in history)
            # 调用模型服务，并等待模型返回文本
            tracer = get_tracer(__name__)
            with tracer.start_as_current_span("llm.complete") as span:
                span.set_attribute("aegis.run_id", str(run.id))
                span.set_attribute("gen_ai.provider.name", self._llm_client.provider_name)
                span.set_attribute("gen_ai.request.model", self._llm_client.model_name)
                reply = await self._llm_client.complete(messages)

            await self._lifecycle.complete_run(
                run,
                context,
                assistant_content=reply,
                step_id=step_id,
            )
            return AgentExecutionResult(status="completed")
        except asyncio.CancelledError:
            raise
        except LLMError as error:
            await self._lifecycle.fail_run(
                run,
                step_id=step_id,
                error_code="LLM_UNAVAILABLE",
                error_message=str(error),
            )
            return AgentExecutionResult(
                status="failed", error_code="LLM_UNAVAILABLE", error_message=str(error)
            )
        except Exception:
            # 仅写入 Worker 本地日志以帮助开发排错；数据库和接口仍使用脱敏错误文本。
            logger.exception("Agent 执行失败：run_id=%s", run.id)
            await self._lifecycle.fail_run(
                run,
                step_id=step_id,
                error_code="AGENT_EXECUTION_FAILED",
                error_message="任务执行失败",
            )
            return AgentExecutionResult(
                status="failed", error_code="AGENT_EXECUTION_FAILED", error_message="任务执行失败"
            )

    async def _execute_selected_tool(self, run, context: TenantContext, history) -> str | None:
        """选择并执行一期只读日历工具；工具故障转为可见结果，不中断普通 LLM 回复。"""
        if self._tool_execution is None:
            return None
        latest_user_message = next(
            (item["content"] for item in reversed(history) if item["role"] == "user"), None
        )
        if not isinstance(latest_user_message, str):
            return None
        invocation = self._tool_router.select(latest_user_message)
        if invocation is None:
            return None
        return await self._tool_execution.invoke_read_tool(run, context, invocation)
