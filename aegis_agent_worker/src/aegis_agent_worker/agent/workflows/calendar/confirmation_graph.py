"""日历写入的 LangGraph 确认工作流。

Graph 只保存流程恢复状态；日历草稿、审批项、工具调用和事件仍由业务表保存。
"""

from typing import Any, Awaitable, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.specialists.calendar_agent import CalendarAgent
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.service.calendar.calendar_confirmation_service import CalendarConfirmationService
from uuid import UUID


class CalendarWorkflowState(TypedDict, total=False):
    run_id: str
    draft_id: str
    approval_item_id: str
    availability: dict[str, Any]
    decision: str
    external_event_id: str


LoadContext = Callable[[CalendarWorkflowState], Awaitable[dict[str, Any]]]
QueryAvailability = Callable[[CalendarWorkflowState], Awaitable[dict[str, Any]]]
CreateDraft = Callable[[CalendarWorkflowState], Awaitable[dict[str, Any]]]
CreateApproval = Callable[[CalendarWorkflowState], Awaitable[dict[str, Any]]]
CreateEvent = Callable[[CalendarWorkflowState], Awaitable[dict[str, Any]]]


class CalendarConfirmationWorkflow:
    """会议草稿、逐项确认和创建日历事件的领域子图。"""

    def __init__(
        self,
        calendar_agent: CalendarAgent,
        confirmation_service: CalendarConfirmationService | None,
    ) -> None:
        self._calendar_agent = calendar_agent
        self._confirmation_service = confirmation_service

    def build(self) -> Any:
        """构建可嵌入根图的会议确认流程。"""
        graph = StateGraph(TaskGraphState)
        graph.add_node("prepare_calendar_confirmation", self._prepare)
        graph.add_node("wait_for_calendar_approval", self._wait)
        graph.add_node("finish_calendar_approval", self._finish)
        graph.add_edge(START, "prepare_calendar_confirmation")
        graph.add_edge("prepare_calendar_confirmation", "wait_for_calendar_approval")
        graph.add_edge("wait_for_calendar_approval", "finish_calendar_approval")
        graph.add_edge("finish_calendar_approval", END)
        return graph.compile()

    async def _prepare(self, state: TaskGraphState) -> dict[str, Any]:
        """按日程专家的受控参数生成会议草稿，绝不在此节点写入外部日历。"""
        if self._confirmation_service is None:
            return {"confirmation_error": "会议确认服务尚未配置。"}
        run = _conversation_run_from_state(state)
        message = state.get("latest_user_message", "")
        plan = self._calendar_agent.plan_meeting(message)
        return await self._confirmation_service.prepare(
            run,
            invocation=plan.availability_invocation,
            title=plan.title,
            attendees=plan.attendees,
        )

    @staticmethod
    def _wait(state: TaskGraphState) -> dict[str, str]:
        """在确认项已经生成时暂停；无草稿错误则正常结束为不可用提示。"""
        if not state.get("awaiting_confirmation"):
            return {"approval_decision": "unavailable"}
        decision = interrupt(
            {
                "run_id": state["run_id"],
                "draft_id": state.get("draft_id"),
                "approval_item_id": state.get("approval_item_id"),
                "status": "waiting_confirmation",
            }
        )
        return {"approval_decision": str(decision)}

    async def _finish(self, state: TaskGraphState) -> dict[str, str]:
        """批准后调用唯一允许的写入服务；拒绝时不触发任何外部副作用。"""
        if state.get("approval_decision") == "approved":
            if self._confirmation_service is None:
                raise RuntimeError("会议确认服务尚未配置")
            draft_id = state.get("draft_id")
            approval_item_id = state.get("approval_item_id")
            if not isinstance(draft_id, str) or not isinstance(approval_item_id, str):
                raise RuntimeError("恢复的会议确认图缺少草稿或确认项标识")
            external_event_id = await self._confirmation_service.create_approved_event(
                _conversation_run_from_state(state),
                draft_id=UUID(draft_id),
                approval_item_id=UUID(approval_item_id),
            )
            return {"assistant_content": f"会议已创建到日历，事件标识：{external_event_id}。"}
        return {"assistant_content": "已记录你拒绝创建该会议，日历不会发生任何变更。"}


def _conversation_run_from_state(state: TaskGraphState) -> ClaimedRun:
    """从根图状态还原会议确认所需的对话任务。"""
    conversation_id = state.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise RuntimeError("会议确认任务缺少会话标识")
    return ClaimedRun(
        id=UUID(state["run_id"]),
        tenant_id=UUID(state["tenant_id"]),
        user_id=UUID(state["user_id"]),
        conversation_id=UUID(conversation_id),
        run_type="conversation",
    )


def build_calendar_confirmation_graph(
    *,
    load_context: LoadContext,
    query_availability: QueryAvailability,
    create_draft: CreateDraft,
    create_approval: CreateApproval,
    create_event: CreateEvent,
):
    """构建并返回以 `run_id` 为 thread_id 的日历确认图。"""

    async def wait_for_approval(state: CalendarWorkflowState) -> dict[str, Any]:
        decision = interrupt(
            {
                "run_id": state["run_id"],
                "draft_id": state["draft_id"],
                "approval_item_id": state["approval_item_id"],
                "status": "waiting_approval",
            }
        )
        return {"decision": str(decision)}

    def route_after_approval(state: CalendarWorkflowState) -> str:
        return "create_event" if state.get("decision") == "approved" else "rejected"

    async def rejected(_state: CalendarWorkflowState) -> dict[str, Any]:
        return {}

    graph = StateGraph(CalendarWorkflowState)
    graph.add_node("load_context", load_context)
    graph.add_node("query_availability", query_availability)
    graph.add_node("create_calendar_draft", create_draft)
    graph.add_node("create_approval_item", create_approval)
    graph.add_node("wait_for_approval", wait_for_approval)
    graph.add_node("create_event", create_event)
    graph.add_node("rejected", rejected)
    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "query_availability")
    graph.add_edge("query_availability", "create_calendar_draft")
    graph.add_edge("create_calendar_draft", "create_approval_item")
    graph.add_edge("create_approval_item", "wait_for_approval")
    graph.add_conditional_edges("wait_for_approval", route_after_approval)
    graph.add_edge("create_event", END)
    graph.add_edge("rejected", END)
    return graph
