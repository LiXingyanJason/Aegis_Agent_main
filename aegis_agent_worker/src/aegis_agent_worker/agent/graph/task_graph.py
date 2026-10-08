"""Aegis 根任务图：加载上下文、选择子图、生成最终回复。"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry, WorkflowRegistrar
from aegis_agent_worker.agent.graph.router import TaskGraphRouter
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.config.database import TenantContext
from aegis_agent_worker.llm.client import LLMError, LLMMessage, OpenAICompatibleClient
from aegis_agent_worker.observability.telemetry import get_tracer
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.service.memory.memory_context_service import MemoryContextService
from aegis_agent_worker.service.runtime.conversation_context_service import ConversationContextService
from aegis_agent_worker.service.runtime.run_lifecycle_service import RunLifecycleService


@dataclass(frozen=True, slots=True)
class TaskGraphDependencies:
    """图节点需要的运行期能力；数据库与网络细节保留在服务层。"""

    context_service: ConversationContextService
    lifecycle: RunLifecycleService
    llm_client: OpenAICompatibleClient
    system_prompt: str
    router: TaskGraphRouter
    workflow_factories: tuple[WorkflowRegistrar, ...]
    memory_context_service: MemoryContextService | None = None


class LLMGraphError(LLMError):
    """携带已创建步骤标识的模型异常，供任务生命周期服务正确收尾。"""

    def __init__(self, step_id: UUID, cause: LLMError) -> None:
        super().__init__(str(cause))
        self.step_id = step_id


class TaskGraph:
    """构建根图；领域 Factory 注册子图，根图不持有日历或邮件业务依赖。"""

    def __init__(self, dependencies: TaskGraphDependencies) -> None:
        self._dependencies = dependencies

    def build(self, *, checkpointer: Any | None = None) -> Any:
        """返回已编译的根图。"""
        registry = GraphRegistry()
        for factory in self._dependencies.workflow_factories:
            factory.register(registry)
        registered_workflows = registry.names()

        graph = StateGraph(TaskGraphState)
        graph.add_node("select_run_workflow", self._select_run_workflow)
        graph.add_node("load_context", self._load_context)
        graph.add_node("decide_workflow", self._decide_workflow)
        # 统一将workflow作为子图加载进入主图
        for workflow_name in registered_workflows:
            graph.add_node(workflow_name, registry.get(workflow_name))
        graph.add_node("generate_reply", self._generate_reply)

        run_routes = {"conversation": "load_context"}
        if "mail_extraction" in registered_workflows:
            run_routes["mail_extraction"] = "mail_extraction"
        if "mail_reply_draft" in registered_workflows:
            run_routes["mail_reply_draft"] = "mail_reply_draft"
        if "mail_send_confirmation" in registered_workflows:
            run_routes["mail_send_confirmation"] = "mail_send_confirmation"

        # 先执行节点 select_run_workflow。
        # 执行完后，调用 self._route_run_type(state)，根据当前图状态返回一个字符串
        # 然后根据run_routes = {
        #     "conversation": "load_context",
        #     "mail_extraction": "mail_extraction",
        #     "mail_reply_draft": "mail_reply_draft",
        #     "mail_send_confirmation": "mail_send_confirmation",
        # }选择下一节点
        graph.add_edge(START, "select_run_workflow")
        graph.add_conditional_edges(
            "select_run_workflow",
            self._route_run_type,
            run_routes,
        )
        graph.add_edge("load_context", "decide_workflow")
        conversation_routes = {"general_reply": "generate_reply"}
        if "calendar_read" in registered_workflows:
            conversation_routes["calendar_read"] = "calendar_read"
        if "calendar_confirmation" in registered_workflows:
            conversation_routes["calendar_confirmation"] = "calendar_confirmation"
        graph.add_conditional_edges(
            "decide_workflow",
            self._select_workflow,
            conversation_routes,
        )
        if "calendar_read" in registered_workflows:
            graph.add_edge("calendar_read", "generate_reply")
        if "calendar_confirmation" in registered_workflows:
            graph.add_edge("calendar_confirmation", END)
        if "mail_extraction" in registered_workflows:
            graph.add_edge("mail_extraction", END)
        if "mail_reply_draft" in registered_workflows:
            graph.add_edge("mail_reply_draft", END)
        if "mail_send_confirmation" in registered_workflows:
            graph.add_edge("mail_send_confirmation", END)
        graph.add_edge("generate_reply", END)
        return graph.compile(checkpointer=checkpointer)

    @staticmethod
    def _select_run_workflow(state: TaskGraphState) -> dict[str, str]:
        """
        将数据库中的 run_type 转为受限的根图工作流名称。
        run_type 表示“这一次 Agent 任务属于什么业务类型”。
                例如：
                conversation：	用户在主对话框发送消息	普通自然语言任务	加载上下文，再进行意图路由
                mail_extraction：	点击某封邮件的“生成摘要”	对指定邮件提取关键信息、待办、截止时间候选	直接进入邮件摘要子图
        """
        run_type = state.get("run_type", "conversation")
        mapping = {
            "mail_extraction": "mail_extraction",
            "mail_reply_draft": "mail_reply_draft",
            "mail_send": "mail_send_confirmation",
        }
        return {"workflow": mapping.get(run_type, "conversation")}

    @staticmethod
    def _route_run_type(state: TaskGraphState) -> str:
        """根图启动后优先按可信 run_type 分流，邮件任务不读取对话上下文。"""
        return state.get("workflow", "conversation")

    async def _load_context(self, state: TaskGraphState) -> dict[str, Any]:
        """
        读取租户隔离的会话消息，并提供根路由所需的最新用户消息。
        长期记忆：Worker 创建并传入 MemoryContextService 调用 MemoryContextService 获得长期记忆
        """
        run = _claimed_run_from_state(state)
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        history = await self._dependencies.context_service.load_history(run, context)
        latest_user_message = next(
            (item["content"] for item in reversed(history) if item.get("role") == "user"), ""
        )

        # 加载用户长期记忆
        memory_context = None
        if self._dependencies.memory_context_service is not None:
            selected_memories = await self._dependencies.memory_context_service.select_for_run(
                run_id=run.id, context=context, task_text=latest_user_message
            )
            memory_context = self._dependencies.memory_context_service.format_system_context(
                selected_memories
            )

        # langgraph自动将{"history": history, "latest_user_message": latest_user_message}覆盖state同名字段
        # 等价 == state["history"] = history
        #        state["latest_user_message"] = latest_user_message
        return {
            "history": history,
            "latest_user_message": latest_user_message,
            "memory_context": memory_context or "",
        }

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
        memory_context = state.get("memory_context")
        if memory_context:
            messages.append(LLMMessage(role="system", content=memory_context))
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

def _claimed_run_from_state(state: TaskGraphState) -> ClaimedRun:
    """从根图状态重建服务层需要的任务归属对象。"""
    return ClaimedRun(
        id=UUID(state["run_id"]),
        tenant_id=UUID(state["tenant_id"]),
        user_id=UUID(state["user_id"]),
        conversation_id=UUID(state["conversation_id"]),
    )
