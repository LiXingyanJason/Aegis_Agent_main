"""Aegis 根任务图：加载上下文、选择子图、生成最终回复。"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry
from aegis_agent_worker.agent.graph.router import TaskGraphRouter
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.specialists.scheduler_agent import SchedulerAgent
from aegis_agent_worker.agent.workflows.calendar.read_graph import CalendarReadWorkflow
from aegis_agent_worker.config.database import TenantContext
from aegis_agent_worker.llm.client import LLMError, LLMMessage, OpenAICompatibleClient
from aegis_agent_worker.observability.telemetry import get_tracer
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.service.conversation_context_service import ConversationContextService
from aegis_agent_worker.service.calendar_confirmation_service import CalendarConfirmationService
from aegis_agent_worker.service.run_lifecycle_service import RunLifecycleService
from aegis_agent_worker.service.tool_execution_service import ToolExecutionService


@dataclass(frozen=True, slots=True)
class TaskGraphDependencies:
    """图节点需要的运行期能力；数据库与网络细节保留在服务层。"""

    context_service: ConversationContextService
    lifecycle: RunLifecycleService
    llm_client: OpenAICompatibleClient
    scheduler_agent: SchedulerAgent
    tool_execution: ToolExecutionService | None
    calendar_confirmation: CalendarConfirmationService | None
    system_prompt: str
    router: TaskGraphRouter


class LLMGraphError(LLMError):
    """携带已创建步骤标识的模型异常，供任务生命周期服务正确收尾。"""

    def __init__(self, step_id: UUID, cause: LLMError) -> None:
        super().__init__(str(cause))
        self.step_id = step_id


class TaskGraph:
    """构建一次任务的根图，并将日历读取作为已注册子图接入。"""

    def __init__(self, dependencies: TaskGraphDependencies) -> None:
        self._dependencies = dependencies

    def build(self, *, checkpointer: Any | None = None) -> Any:
        """返回已编译的根图。"""
        registry = GraphRegistry()
        registry.register( # calendar_read 创建子图注册表
            "calendar_read",
            CalendarReadWorkflow(
                self._dependencies.scheduler_agent,
                self._dependencies.tool_execution,
            ).build(),
            # 读取最新用户消息
            # → SchedulerAgent 按规则选择日历工具
            # → ToolExecutionService 执行工具
            # → 将可信工具结果写入图状态 tool_context
        )

        graph = StateGraph(TaskGraphState)
        graph.add_node("load_context", self._load_context)
        graph.add_node("decide_workflow", self._decide_workflow)
        graph.add_node("calendar_read", registry.get("calendar_read"))
        graph.add_node("calendar_confirmation", self._prepare_calendar_confirmation)
        graph.add_node("wait_for_calendar_approval", self._wait_for_calendar_approval)
        graph.add_node("finish_calendar_approval", self._finish_calendar_approval)
        graph.add_node("generate_reply", self._generate_reply)
        graph.add_edge(START, "load_context")
        graph.add_edge("load_context", "decide_workflow")
        graph.add_conditional_edges(
            "decide_workflow",
            self._select_workflow,
            {
                "calendar_read": "calendar_read",
                "calendar_confirmation": "calendar_confirmation",
                "general_reply": "generate_reply",
            },
        )
        graph.add_edge("calendar_read", "generate_reply")
        graph.add_edge("calendar_confirmation", "wait_for_calendar_approval")
        graph.add_edge("wait_for_calendar_approval", "finish_calendar_approval")
        graph.add_edge("finish_calendar_approval", END)
        graph.add_edge("generate_reply", END)
        return graph.compile(checkpointer=checkpointer)

    async def _load_context(self, state: TaskGraphState) -> dict[str, Any]:
        """读取租户隔离的会话消息，并提供根路由所需的最新用户消息。"""
        run = _claimed_run_from_state(state)
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        history = await self._dependencies.context_service.load_history(run, context)
        latest_user_message = next(
            (item["content"] for item in reversed(history) if item.get("role") == "user"), ""
        )
        # langgraph自动将{"history": history, "latest_user_message": latest_user_message}覆盖state同名字段
        # 等价 == state["history"] = history
        #        state["latest_user_message"] = latest_user_message
        return {"history": history, "latest_user_message": latest_user_message}

    def _decide_workflow(self, state: TaskGraphState) -> dict[str, str]:
        """记录意图及选中的子图名称，便于后续审计和恢复。"""
        return self._dependencies.router.decide(state)

    @staticmethod
    def _select_workflow(state: TaskGraphState) -> str:
        """读取路由节点写入的工作流名称并选择 LangGraph 下一节点。"""
        return state.get("workflow", "general_reply")

    async def _generate_reply(self, state: TaskGraphState) -> dict[str, str]:
        """调用模型生成最终回复；步骤记录仍交由生命周期服务处理。"""
        run = _claimed_run_from_state(state)
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        step_id = await self._dependencies.lifecycle.start_llm_generation(run, context)
        messages = [LLMMessage(role="system", content=self._dependencies.system_prompt)]
        tool_context = state.get("tool_context")
        if tool_context:
            messages.append(LLMMessage(role="system", content=tool_context))
        messages.extend(
            LLMMessage(role=item["role"], content=item["content"])
            for item in state.get("history", [])
        )
        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("llm.complete") as span:
            span.set_attribute("aegis.run_id", str(run.id))
            span.set_attribute("gen_ai.provider.name", self._dependencies.llm_client.provider_name)
            span.set_attribute("gen_ai.request.model", self._dependencies.llm_client.model_name)
            try:
                reply = await self._dependencies.llm_client.complete(messages)
            except LLMError as error:
                raise LLMGraphError(step_id, error) from error
        return {"llm_step_id": str(step_id), "assistant_content": reply}

    async def _prepare_calendar_confirmation(self, state: TaskGraphState) -> dict[str, Any]:
        """生成会议草稿并发出确认事件；此节点绝不执行日历写入。"""
        if self._dependencies.calendar_confirmation is None:
            return {"confirmation_error": "会议确认服务尚未配置。"}
        run = _claimed_run_from_state(state)
        message = state.get("latest_user_message", "")
        invocation = self._dependencies.scheduler_agent.select_meeting_availability_tool(message)
        return await self._dependencies.calendar_confirmation.prepare(
            run,
            invocation=invocation,
            title=self._dependencies.scheduler_agent.meeting_title(message),
            attendees=invocation.arguments.get("participants", []),
        )

    @staticmethod
    def _wait_for_calendar_approval(state: TaskGraphState) -> dict[str, str]:
        """暂停图并持久化 Checkpoint；恢复值只能是批准或拒绝决定。"""
        if not state.get("awaiting_confirmation"):
            return {"approval_decision": "unavailable"}
        # 我们编写了分支路由，在需要暂停的时候路由到该节点执行该函数 框架自动帮我们用当前 runid 存Checkpointer，并返回interrupt？
        decision = interrupt(
            {
                "run_id": state["run_id"],
                "draft_id": state.get("draft_id"),
                "approval_item_id": state.get("approval_item_id"),
                "status": "waiting_confirmation",
            }
        )
        return {"approval_decision": str(decision)}

    async def _finish_calendar_approval(self, state: TaskGraphState) -> dict[str, str]:
        """批准后经确认服务执行唯一允许的日历写入；拒绝不调用工具。"""
        if state.get("approval_decision") == "approved":
            if self._dependencies.calendar_confirmation is None:
                raise RuntimeError("会议确认服务尚未配置")
            run = _claimed_run_from_state(state)
            draft_id = state.get("draft_id")
            approval_item_id = state.get("approval_item_id")
            if not isinstance(draft_id, str) or not isinstance(approval_item_id, str):
                raise RuntimeError("恢复的会议确认图缺少草稿或确认项标识")
            external_event_id = await self._dependencies.calendar_confirmation.create_approved_event(
                run, draft_id=UUID(draft_id), approval_item_id=UUID(approval_item_id)
            )
            return {"assistant_content": f"会议已创建到日历，事件标识：{external_event_id}。"}
        return {"assistant_content": "已记录你拒绝创建该会议，日历不会发生任何变更。"}


def _claimed_run_from_state(state: TaskGraphState) -> ClaimedRun:
    """从根图状态重建服务层需要的任务归属对象。"""
    return ClaimedRun(
        id=UUID(state["run_id"]),
        tenant_id=UUID(state["tenant_id"]),
        user_id=UUID(state["user_id"]),
        conversation_id=UUID(state["conversation_id"]),
    )
