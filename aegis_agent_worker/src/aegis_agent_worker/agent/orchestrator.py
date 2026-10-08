"""Agent 任务入口：领取任务、运行根图并持久化最终结果。"""

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from aegis_agent_worker.agent.graph.graph_runner import GraphRunner
from aegis_agent_worker.agent.graph.graph_registry import WorkflowRegistrar
from aegis_agent_worker.agent.graph.router import TaskGraphRouter
from aegis_agent_worker.agent.graph.task_graph import LLMGraphError, TaskGraph, TaskGraphDependencies
from aegis_agent_worker.config.database import Database, TenantContext
from aegis_agent_worker.event.run_event import RunEventPublisher
from aegis_agent_worker.llm.client import LLMError, OpenAICompatibleClient
from aegis_agent_worker.repository.run_repository import AgentRunRepository
from aegis_agent_worker.service.mail.mail_task_service import MailTaskService
from aegis_agent_worker.service.memory.memory_context_service import MemoryContextService
from aegis_agent_worker.service.runtime.conversation_context_service import ConversationContextService
from aegis_agent_worker.service.runtime.run_lifecycle_service import RunLifecycleService

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
        lifecycle: RunLifecycleService | None = None, # 任务生命周期服务 记录任务事件 queued → running → completed / failed
        context_service: ConversationContextService | None = None, # 会话上下文服务。它从数据库读取当前任务对应会话的历史消息，供根图和 LLM 组织上下文。
        workflow_factories: tuple[WorkflowRegistrar, ...] = (), # 各领域自行组装并注册子图
        graph_runner: GraphRunner | None = None, # LangGraph 根图的执行器 执行或未来恢复同一任务图
        checkpointer=None, # 检查点
        mail_tasks: MailTaskService | None = None,
        memory_context: MemoryContextService | None = None,
    ) -> None:
        """组装根图及其依赖；允许测试注入服务或预构建运行器。"""
        run_repository = runs or AgentRunRepository()
        self._llm_client = llm_client
        self._mail_tasks = mail_tasks
        self._lifecycle = lifecycle or RunLifecycleService(database, run_repository, events)
        self._context_service = context_service or ConversationContextService(database, run_repository)
        self._graph_runner = graph_runner or GraphRunner(
            TaskGraph(
                TaskGraphDependencies(
                    context_service=self._context_service,
                    lifecycle=self._lifecycle,
                    llm_client=llm_client,
                    system_prompt=self._system_prompt,
                    router=TaskGraphRouter(),
                    workflow_factories=workflow_factories,
                    memory_context_service=memory_context,
                )
            ).build(checkpointer=checkpointer)
        )

    async def execute(self, run_id: UUID, *, resume_decision: str | None = None) -> AgentExecutionResult:
        """领取任务、执行根图，并返回 Worker 所需的最终状态。"""
        # 判断 Redis 消息是否包含审批决定
        if resume_decision is None:
            # 没有 resume_decision 时，是普通新任务。原子更新数据库、记录模型供应商、模型名称、开始时间和 run_started 事件
            """
                run 是一个class ClaimedRun: 对象               
                    id: UUID
                    tenant_id: UUID
                    user_id: UUID
                    conversation_id: UUID
            """
            run = await self._lifecycle.claim_queued_run(
                run_id,
                model_provider=self._llm_client.provider_name,
                model_name=self._llm_client.model_name,
            )
        else: #有 resume_decision 时，这是审批后的恢复任务 核验信息
            run = await self._lifecycle.resume_waiting_confirmation_run(
                run_id, decision=resume_decision
            )
        if run is None:
            return AgentExecutionResult(status="ignored")

        step_id: UUID | None = None
        try:
            """
               普通任务:
                → invoke(run)
                → 从 START 开始运行根图。
               恢复任务:
                → resume(run.id, approved/rejected)
                → Checkpointer 从 PostgreSQL 读取该 run_id 的图快照
                → 从 interrupt(...) 之后继续执行。
            """
            if resume_decision is not None:
                # 审批后的恢复任务：由 Checkpointer 按 run_id 恢复到 interrupt 之后。
                state = await self._graph_runner.resume(str(run.id), resume_decision)
            else:
                # 首次提交的普通任务：从根图的 START 节点开始执行。
                state = await self._graph_runner.invoke(run)
                # 执行时框架遇到暂停点，自动帮我们用当前run_id存Checkpointer，并返回interrupt
            if state.get("__interrupt__"): # 图执行到审批暂停点时，LangGraph 返回 __interrupt__
                return AgentExecutionResult(status="waiting_confirmation")
            # 邮件后台子图会自行保存领域结果及 run_completed 事件，不写入对话消息。
            if state.get("mail_workflow_completed"):
                return AgentExecutionResult(status="completed")
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
            if run.run_type in {"mail_extraction", "mail_reply_draft"} and self._mail_tasks is not None:
                await self._mail_tasks.mark_failed(run, str(error))
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
            if run.run_type in {"mail_extraction", "mail_reply_draft"} and self._mail_tasks is not None:
                await self._mail_tasks.mark_failed(run, str(error))
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
            if run.run_type in {"mail_extraction", "mail_reply_draft"} and self._mail_tasks is not None:
                await self._mail_tasks.mark_failed(run, "任务执行失败")
            await self._lifecycle.fail_run(
                run,
                step_id=step_id,
                error_code="AGENT_EXECUTION_FAILED",
                error_message="任务执行失败",
            )
            return AgentExecutionResult(
                status="failed", error_code="AGENT_EXECUTION_FAILED", error_message="任务执行失败"
            )
