"""Agent 任务入口：领取任务、运行根图并持久化最终结果。"""

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from aegis_agent_worker.agent.graph.graph_runner import GraphRunner
from aegis_agent_worker.agent.graph.router import TaskGraphRouter
from aegis_agent_worker.agent.graph.task_graph import LLMGraphError, TaskGraph, TaskGraphDependencies
from aegis_agent_worker.agent.specialists.scheduler_agent import SchedulerAgent
from aegis_agent_worker.config.database import Database, TenantContext
from aegis_agent_worker.event.run_event import RunEventPublisher
from aegis_agent_worker.llm.client import LLMError, OpenAICompatibleClient
from aegis_agent_worker.repository.run_repository import AgentRunRepository
from aegis_agent_worker.repository.tool_repository import ToolCallRepository
from aegis_agent_worker.service.conversation_context_service import ConversationContextService
from aegis_agent_worker.service.run_lifecycle_service import RunLifecycleService
from aegis_agent_worker.service.tool_execution_service import ToolExecutionService
from aegis_agent_worker.tool.gateway import ToolGateway

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    """Worker 输出所需的任务执行结果；错误信息已在业务层脱敏。"""

    status: str
    error_code: str | None = None
    error_message: str | None = None


class AgentOrchestrator:
    """消费一个 queued 任务并运行 LangGraph 根图；持久化由应用服务负责。"""

    _system_prompt = "你是 Aegis PA 个人效率助手。请基于当前会话，给出准确、简洁、可执行的中文回复。"

    def __init__(
        self,
        database: Database, # 数据库连接，传给 RunLifecycleService、等 Service 开启事务、调用 Repository。
        llm_client: OpenAICompatibleClient, # 模型通信客户端
        runs: AgentRunRepository | None = None, # 任务相关持久化数据库仓储
        events: RunEventPublisher | None = None, # Redis 事件发布器
        tools: ToolGateway | None = None, # 工具网关，负责查找工具、校验参数与权限、准备调用上下文、通过 MCP Client 调用外部日历服务
        tool_calls: ToolCallRepository | None = None, # 工具调用审计仓储，负责保存 tool_calls 表记录
        scheduler_agent: SchedulerAgent | None = None, # 日程专职 Agent
        lifecycle: RunLifecycleService | None = None, # 任务生命周期服务 记录任务事件 queued → running → completed / failed
        context_service: ConversationContextService | None = None, # 会话上下文服务。它从数据库读取当前任务对应会话的历史消息，供根图和 LLM 组织上下文。
        tool_execution: ToolExecutionService | None = None, # 工具调用用例服务，负责把一次工具调用组织成完整业务动作 创建 run_step 创建 tool_call 审计记录校验并调用 ToolGateway保存结果或错误 写入 tool_preview / progress_updated 事件
        graph_runner: GraphRunner | None = None, # LangGraph 根图的执行器 执行或未来恢复同一任务图
    ) -> None:
        """组装根图及其依赖；允许测试注入服务或预构建运行器。"""
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
        self._graph_runner = graph_runner or GraphRunner(
            TaskGraph(
                TaskGraphDependencies(
                    context_service=self._context_service,
                    lifecycle=self._lifecycle,
                    llm_client=llm_client,
                    scheduler_agent=scheduler_agent or SchedulerAgent(),
                    tool_execution=self._tool_execution,
                    system_prompt=self._system_prompt,
                    router=TaskGraphRouter(),
                )
            ).build()
        )

    async def execute(self, run_id: UUID) -> AgentExecutionResult:
        """领取任务、执行根图，并返回 Worker 所需的最终状态。"""
        run = await self._lifecycle.claim_queued_run(
            run_id,
            model_provider=self._llm_client.provider_name,
            model_name=self._llm_client.model_name,
        ) # “原子领取”一个等待执行的任务 若为空则是被其他worker抢走了，直接返回AgentExecutionResult(status="ignored")
        if run is None:
            return AgentExecutionResult(status="ignored")

        step_id: UUID | None = None
        try:
            state = await self._graph_runner.invoke(run)
            assistant_content = state.get("assistant_content")
            if not isinstance(assistant_content, str) or not assistant_content:
                raise RuntimeError("根图未生成助手回复")
            raw_step_id = state.get("llm_step_id")
            if isinstance(raw_step_id, str):
                step_id = UUID(raw_step_id)
            await self._lifecycle.complete_run(
                run,
                context=TenantContext(tenant_id=run.tenant_id, user_id=run.user_id),
                assistant_content=assistant_content,
                step_id=step_id,
            )
            return AgentExecutionResult(status="completed")
        except asyncio.CancelledError:
            raise
        except LLMGraphError as error:
            await self._lifecycle.fail_run(
                run,
                step_id=error.step_id,
                error_code="LLM_UNAVAILABLE",
                error_message=str(error),
            )
            return AgentExecutionResult(
                status="failed", error_code="LLM_UNAVAILABLE", error_message=str(error)
            )
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
