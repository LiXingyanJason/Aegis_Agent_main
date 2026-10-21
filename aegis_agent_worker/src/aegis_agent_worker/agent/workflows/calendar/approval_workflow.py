"""确定性确认续接，无Agent或LLM依赖；独立安排与团队入口共享等待/最终写入。"""
from uuid import UUID

from langgraph.graph import START, END, StateGraph
from langgraph.types import interrupt

from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.event.cooperation_context import association, cooperation_event_scope


def calendar_run(state):
    return ClaimedRun(id=UUID(state["run_id"]),tenant_id=UUID(state["tenant_id"]),user_id=UUID(state["user_id"]),
        conversation_id=UUID(state["conversation_id"]),run_type="conversation")


class CalendarApprovalWorkflow:
    def __init__(self, confirmation_service, draft_service=None):
        self._confirmation, self._drafts = confirmation_service, draft_service

    def build(self):
        if self._drafts is None or self._confirmation is None:
            raise ValueError("团队审批入口必须配置草稿事实服务及确定性写入服务")
        graph = StateGraph(TaskGraphState)
        graph.add_node("prepare_saved_calendar_approval", self._prepare)
        graph.add_node("wait_for_calendar_approval", self.wait)
        graph.add_node("finish_calendar_approval", self.finish)
        graph.add_edge(START,"prepare_saved_calendar_approval")
        graph.add_edge("prepare_saved_calendar_approval","wait_for_calendar_approval")
        graph.add_edge("wait_for_calendar_approval","finish_calendar_approval")
        graph.add_edge("finish_calendar_approval",END)
        return graph.compile()

    async def _prepare(self, state):
        return await self._drafts.prepare_approval(calendar_run(state),state["cooperation_draft"])

    @staticmethod
    def wait(state):
        if not state.get("awaiting_confirmation"):
            return {"approval_decision":"unavailable"}
        payload = dict(run_id=state["run_id"],draft_id=state.get("draft_id"),
            approval_item_id=state.get("approval_item_id"),status="waiting_confirmation")
        if state.get("approval_ready_required"):
            payload.update(approval_ready_required=True,draft_version=state["draft_version"],
                           cooperation=association(state["run_id"],"approval"))
        decision = interrupt(payload)
        if not isinstance(decision,str) or decision not in {"approved","rejected"}:
            raise HarnessError("APPROVAL_DECISION_INVALID","确认决定无效，未写入日历")
        return {"approval_decision":decision}

    async def finish(self, state):
        if state.get("confirmation_error"):
            return {"assistant_content":state["confirmation_error"]}
        if state.get("approval_decision") == "approved":
            if self._confirmation is None:
                raise RuntimeError("会议确认服务尚未配置")
            if self._drafts is not None:
                await self._drafts.validate_approval_resume(calendar_run(state), state["cooperation_draft"],
                    UUID(state["approval_item_id"]))
            if self._drafts is not None:
                with cooperation_event_scope(calendar_run(state),association(state["run_id"],"approval")):
                    event_id = await self._confirmation.create_approved_event(calendar_run(state),
                        draft_id=UUID(state["draft_id"]),approval_item_id=UUID(state["approval_item_id"]))
            else:
                event_id = await self._confirmation.create_approved_event(calendar_run(state),
                    draft_id=UUID(state["draft_id"]),approval_item_id=UUID(state["approval_item_id"]))
            return {"assistant_content":f"会议已创建到日历，事件标识：{event_id}。"}
        if state.get("approval_decision") == "rejected":
            return {"assistant_content":"已记录你拒绝创建该会议，日历不会发生任何变更。"}
        return {"assistant_content":"未生成可确认的会议草稿，日历不会发生任何变更。"}
