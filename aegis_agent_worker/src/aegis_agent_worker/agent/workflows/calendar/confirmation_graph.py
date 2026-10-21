"""日历写入的 LangGraph 确认工作流。

Graph 只保存流程恢复状态；日历草稿、审批项、工具调用和事件仍由业务表保存。
"""

from typing import Any, Awaitable, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.contracts.calendar import (
    CalendarFreeTimeCandidate,
    CalendarMeetingDraftProposal,
)
from aegis_agent_worker.agent.specialists.calendar_agent import CalendarAgent
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.service.calendar.calendar_confirmation_service import CalendarConfirmationService
from aegis_agent_worker.tool.contracts import ToolInvocation
from uuid import UUID
from datetime import datetime
from aegis_agent_worker.agent.contracts.calendar import CalendarDecision
from aegis_agent_worker.agent.workflows.calendar.planning_workflow import CalendarPlanningWorkflow
from aegis_agent_worker.agent.contracts.decision import AgentHandoff
from aegis_agent_worker.llm.memory_scope import memory_call_scope
from aegis_agent_worker.agent.workflows.calendar.approval_workflow import CalendarApprovalWorkflow


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
        planning_audit=None,
        memory_context=None,
    ) -> None:
        self._calendar_agent = calendar_agent
        self._confirmation_service = confirmation_service
        self._planning_audit = planning_audit
        self._memory_context=memory_context

    def build(self) -> Any:
        """构建“查询候选→模型推荐→草稿确认→写入”的会议安排流程。"""
        graph = StateGraph(TaskGraphState)
        graph.add_node("calendar_planning", CalendarPlanningWorkflow(self._calendar_agent, self._planning_audit,self._memory_context).build())
        graph.add_node("prepare_calendar_confirmation", self._prepare)
        graph.add_node("query_calendar_availability", self._query_availability)
        graph.add_node("generate_calendar_draft", self._generate_draft)
        graph.add_node("create_calendar_draft", self._create_draft)
        graph.add_node("wait_for_calendar_approval", self._wait)
        graph.add_node("finish_calendar_approval", self._finish)
        graph.add_edge(START, "calendar_planning")
        graph.add_edge("calendar_planning", "prepare_calendar_confirmation")
        graph.add_edge("prepare_calendar_confirmation", "query_calendar_availability")
        graph.add_conditional_edges(
            "query_calendar_availability",
            self._route_after_availability,
            {"generate_draft": "generate_calendar_draft", "finish": "finish_calendar_approval"},
        )
        graph.add_conditional_edges(
            "generate_calendar_draft",
            self._route_after_draft_generation,
            {"create_draft": "create_calendar_draft", "finish": "finish_calendar_approval"},
        )
        graph.add_edge("create_calendar_draft", "wait_for_calendar_approval")
        graph.add_edge("wait_for_calendar_approval", "finish_calendar_approval")
        graph.add_edge("finish_calendar_approval", END)
        return graph.compile()

    async def _prepare(self, state: TaskGraphState) -> dict[str, Any]:
        """解析会议请求；后续节点才查询事实候选并生成草稿。"""
        if self._confirmation_service is None:
            return {"confirmation_error": "会议确认服务尚未配置。"}
        if state.get("calendar_planning_error"):
            return {"confirmation_error": state["calendar_planning_error"]}
        run = _conversation_run_from_state(state)
        message = state.get("latest_user_message", "")
        if state.get("handoff"):
            handoff = AgentHandoff.model_validate(state["handoff"])
            if handoff.domain != "calendar" or handoff.intent != "arrange_meeting":
                return {"confirmation_error": "会议交接计划无效。"}
            message = handoff.instruction
        try:
            decision = CalendarDecision.model_validate(state["calendar_decision"])
            if decision.action != "prepare_meeting":
                raise ValueError("会议工作流意图无效")
            plan = decision.to_read_plan()
        except ValueError:
            return {"confirmation_error": "请检查会议日期和起止时间，结束时间须晚于开始时间。未注明时区时按北京时间处理。"}
        return {
            "calendar_meeting_plan": {
                "title": decision.title,
                "attendees": decision.attendees,
                "instruction": message,
                "availability_tool_name": plan.tool_name,
                "availability_arguments": plan.arguments,
            }
        }

    async def _query_availability(self, state: TaskGraphState) -> dict[str, Any]:
        """调用 MCP 取得多个可信空闲候选；此阶段不允许选择或写入日历。"""
        if state.get("confirmation_error"):
            return {}
        if self._confirmation_service is None:
            return {"confirmation_error": "会议确认服务尚未配置。"}
        plan = state.get("calendar_meeting_plan")
        if not isinstance(plan, dict):
            return {"confirmation_error": "会议安排计划缺失，无法查询可用时间。"}
        tool_name = plan.get("availability_tool_name")
        arguments = plan.get("availability_arguments")
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            return {"confirmation_error": "会议安排计划无效，无法查询可用时间。"}
        return await self._confirmation_service.query_available_slots(
            _conversation_run_from_state(state),
            invocation=ToolInvocation(tool_name=tool_name, arguments=arguments),
            decision=CalendarDecision.model_validate(state["calendar_decision"]),
        )

    async def _generate_draft(self, state: TaskGraphState) -> dict[str, Any]:
        """让 LLM 生成受 Schema 和可信候选约束的站内会议草稿。"""
        availability = state.get("availability")
        plan = state.get("calendar_meeting_plan")
        if not isinstance(availability, dict) or not isinstance(plan, dict):
            return {"confirmation_error": "未取得可用于推荐的空闲时间。"}
        raw_candidates = availability.get("candidates")
        if not isinstance(raw_candidates, list):
            return {"confirmation_error": "可用时间候选格式无效。"}
        try:
            candidates = [CalendarFreeTimeCandidate.model_validate(item) for item in raw_candidates]
        except ValueError:
            return {"confirmation_error": "可用时间候选校验失败。"}
        try:
            suggested_title = plan.get("title")
            extracted_attendees = plan.get("attendees")
            if not isinstance(suggested_title, str) or not isinstance(extracted_attendees, list):
                raise ValueError("会议草稿计划无效")
            instruction=plan.get("instruction", state.get("latest_user_message", ""))
            with memory_call_scope(self._memory_context,_conversation_run_from_state(state),
                    'calendar_draft_personalization',instruction):
                proposal = await self._calendar_agent.generate_meeting_draft(
                    user_content=instruction,
                    candidates=candidates,
                    duration_minutes=availability.get("duration_minutes"),
                    suggested_title=suggested_title,
                    extracted_attendees=[item for item in extracted_attendees if isinstance(item, str)],
                    timezone_name=state.get('calendar_decision',{}).get('timezone'),
                )
        except ValueError:
            return {"confirmation_error": "会议草稿未通过内容或候选时段校验，尚未创建。请补充会议要求后重试。"}
        return {"calendar_meeting_draft": proposal.model_dump(mode="json")}

    async def _create_draft(self, state: TaskGraphState) -> dict[str, Any]:
        """将已验证的推荐候选保存为站内草稿，绝不在此节点写入外部日历。"""
        if self._confirmation_service is None:
            return {"confirmation_error": "会议确认服务尚未配置。"}
        plan = state.get("calendar_meeting_plan")
        availability = state.get("availability")
        raw_proposal = state.get("calendar_meeting_draft")
        if not isinstance(plan, dict) or not isinstance(availability, dict) or not isinstance(raw_proposal, dict):
            return {"confirmation_error": "会议草稿所需的推荐数据不完整。"}
        try:
            candidates = [
                CalendarFreeTimeCandidate.model_validate(item)
                for item in availability.get("candidates", [])
            ]
            proposal = CalendarMeetingDraftProposal.model_validate(raw_proposal)
            decision = CalendarDecision.model_validate(state["calendar_decision"])
            candidates_by_id = {candidate.slot_id: candidate for candidate in candidates}
            selected_slot = candidates_by_id[proposal.selected_slot_id]
            alternatives = [
                candidates_by_id[slot_id]
                for slot_id in proposal.alternative_slot_ids
            ]
            duration_minutes = availability.get("duration_minutes")
            if not isinstance(duration_minutes, int):
                raise ValueError("会议时长无效")
            if duration_minutes != decision.duration_minutes:
                raise ValueError("候选时长不符合原计划")
            if set(proposal.attendees) != set(decision.attendees):
                raise ValueError("草稿改变了明确参会人")
            if decision.time_mode == "explicit_interval" and (
                datetime.fromisoformat(selected_slot.start_at) != decision.start_at
                or datetime.fromisoformat(selected_slot.end_at) != decision.end_at
            ):
                raise ValueError("草稿改变了指定时段")
        except (KeyError, TypeError, ValueError):
            return {"confirmation_error": "会议草稿数据不完整或未通过校验，尚未创建。请重新发起会议安排。"}
        return await self._confirmation_service.create_recommended_draft(
            _conversation_run_from_state(state),
            proposal=proposal,
            selected_slot=selected_slot,
            alternatives=alternatives,
            duration_minutes=duration_minutes,
            timezone=decision.timezone,
            scheduling_warning=availability.get("scheduling_warning"),
        )

    @staticmethod
    def _route_after_availability(state: TaskGraphState) -> str:
        """只有可信候选存在时才允许模型进入推荐节点。"""
        return "generate_draft" if state.get("availability") and not state.get("confirmation_error") else "finish"

    @staticmethod
    def _route_after_draft_generation(state: TaskGraphState) -> str:
        """模型草稿须通过 Pydantic 与候选集合校验才能保存。"""
        return (
            "create_draft"
            if state.get("calendar_meeting_draft") and not state.get("confirmation_error")
            else "finish"
        )

    @staticmethod
    def _wait(state: TaskGraphState) -> dict[str, str]:
        """在确认项已经生成时暂停；无草稿错误则正常结束为不可用提示。"""
        return CalendarApprovalWorkflow.wait(state)

    async def _finish(self, state: TaskGraphState) -> dict[str, str]:
        """批准后调用唯一允许的写入服务；拒绝时不触发任何外部副作用。"""
        return await CalendarApprovalWorkflow(self._confirmation_service).finish(state)


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
