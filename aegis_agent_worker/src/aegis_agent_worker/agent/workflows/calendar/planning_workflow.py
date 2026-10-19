"""日历规划子图；推理与动作范围校验在图层，Service 只保存审计。"""

from time import monotonic
from uuid import UUID

from langgraph.graph import START, END, StateGraph

from aegis_agent_worker.agent.contracts.decision import AgentHandoff
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.observability.telemetry import get_tracer
from aegis_agent_worker.agent.specialists.calendar_agent import calendar_validation_issues, CalendarPlanValidationError
from aegis_agent_worker.llm.memory_scope import memory_call_scope
import logging

logger = logging.getLogger(__name__)


class CalendarPlanningWorkflow:
    def __init__(self, calendar_agent, audit_service=None,memory_context=None):
        self._agent = calendar_agent
        self._audit_service = audit_service
        self._memory_context=memory_context

    def build(self):
        graph = StateGraph(TaskGraphState)
        graph.add_node("infer_calendar_plan", self._infer)
        graph.add_edge(START, "infer_calendar_plan")
        graph.add_edge("infer_calendar_plan", END)
        return graph.compile()

    async def _infer(self, state):
        started = monotonic()
        decision = None
        outcome = "validated"
        issues = None
        with get_tracer(__name__).start_as_current_span("agent.calendar.plan") as span:
            span.set_attribute("aegis.run_id", state["run_id"])
            span.set_attribute("gen_ai.request.model", self._agent.model_name)
            try:
                handoff = AgentHandoff.model_validate(state["handoff"])
                run=ClaimedRun(id=UUID(state['run_id']),tenant_id=UUID(state['tenant_id']),
                    user_id=UUID(state['user_id']),conversation_id=UUID(state['conversation_id']) if state.get('conversation_id') else None)
                with memory_call_scope(self._memory_context,run,'calendar_planning',state.get('latest_user_message','')):
                    decision = await self._agent.plan_calendar(
                        user_content=state.get("latest_user_message", ""), handoff=handoff,
                    )
                error = decision.clarification_question if decision.action == "clarify" else ""
            except (KeyError, ValueError) as exception:
                outcome = "invalid_plan"
                issues = (exception.issues if isinstance(exception, CalendarPlanValidationError)
                          else calendar_validation_issues(exception))
                logger.warning("日历计划未通过校验：run_id=%s，issues=%s", state["run_id"], issues)
                error = "日历规划结果未通过校验，尚未查询或创建日程。请重试，或补充明确的会议要求。"
            except Exception:
                await self._audit(state, None, "model_failed", started)
                raise
            await self._audit(state, decision, outcome, started, issues)
        return {
            "calendar_decision": decision.model_dump(mode="json") if decision else {},
            "calendar_planning_error": error or "",
        }

    async def _audit(self, state, decision, outcome, started, issues=None):
        if self._audit_service is not None:
            run = ClaimedRun(id=UUID(state["run_id"]), tenant_id=UUID(state["tenant_id"]),
                             user_id=UUID(state["user_id"]), conversation_id=UUID(state["conversation_id"]))
            await self._audit_service.record_plan(
                run, decision=decision, outcome=outcome,
                duration_ms=round((monotonic() - started) * 1000),
                model_provider=self._agent.provider_name, model_name=self._agent.model_name,
                validation_issues=issues,
            )
