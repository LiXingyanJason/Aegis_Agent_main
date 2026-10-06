"""日历写入的 LangGraph 确认工作流。

Graph 只保存流程恢复状态；日历草稿、审批项、工具调用和事件仍由业务表保存。
"""

from typing import Annotated, Any, Awaitable, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


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
