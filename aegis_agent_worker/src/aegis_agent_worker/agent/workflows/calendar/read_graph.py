"""只读日历 LangGraph 子图：生成计划、执行工具并输出结构化结果。"""

from typing import Any
import json
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from aegis_agent_worker.agent.contracts.calendar import CalendarReadPlan, CalendarDecision, build_calendar_read_artifact
from aegis_agent_worker.agent.workflows.calendar.planning_workflow import CalendarPlanningWorkflow
from aegis_agent_worker.agent.contracts.common import ToolResultSummary
from aegis_agent_worker.agent.specialists.calendar_agent import CalendarAgent
from aegis_agent_worker.agent.workflows.calendar.state import CalendarReadState
from aegis_agent_worker.config.database import TenantContext
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.service.runtime.conversation_artifact_service import ConversationArtifactService
from aegis_agent_worker.service.calendar.calendar_tool_execution_service import CalendarToolExecutionService
from aegis_agent_worker.service.calendar.calendar_confirmation_service import explicit_interval_availability


class CalendarReadWorkflow:
    """日历读取子图只传递受控计划、可信摘要与脱敏 Artifact。"""

    def __init__(
        self,
        calendar_agent: CalendarAgent,
        tool_execution: CalendarToolExecutionService | None,
        artifact_service: ConversationArtifactService | None = None,
        planning_audit=None,
    ) -> None:
        self._calendar_agent = calendar_agent
        self._tool_execution = tool_execution
        self._artifact_service = artifact_service
        self._planning_audit = planning_audit

    def build(self) -> Any:
        """构建可嵌入根图的日历只读子图。"""
        graph = StateGraph(CalendarReadState)
        graph.add_node("calendar_planning", CalendarPlanningWorkflow(self._calendar_agent, self._planning_audit).build())
        graph.add_node("plan_calendar_read", self._plan_calendar_read)
        graph.add_node("execute_calendar_plan", self._execute_calendar_plan)
        graph.add_edge(START, "calendar_planning")
        graph.add_edge("calendar_planning", "plan_calendar_read")
        graph.add_conditional_edges(
            "plan_calendar_read",
            self._route_after_plan,
            {"execute_calendar_plan": "execute_calendar_plan", "finish": END},
        )
        graph.add_edge("execute_calendar_plan", END)
        return graph.compile()

    async def _plan_calendar_read(self, state: CalendarReadState) -> dict[str, Any]:
        """将专家输出转换为 Pydantic 校验后的只读计划。"""
        if state.get("calendar_planning_error"):
            return {"plan": {}}
        try:
            decision = CalendarDecision.model_validate(state["calendar_decision"])
            if decision.action not in {"list_events", "find_free_time"}:
                raise ValueError("读取流程不允许写入意图")
            plan = decision.to_read_plan()
        except (ValidationError, ValueError) as error:
            return {
                "tool_context": "日历查询计划无效，无法安全调用外部日历。",
                "tool_result": ToolResultSummary(
                    status="failed",
                    tool_name="calendar.unspecified",
                    summary={"message": str(error)[:500]},
                ).model_dump(mode="json"),
            }
        return {"plan": plan.model_dump(mode="json")}

    @staticmethod
    def _route_after_plan(state: CalendarReadState) -> str:
        """只有通过 Schema 校验的计划才允许进入外部工具执行节点。"""
        return "execute_calendar_plan" if state.get("plan") else "finish"

    async def _execute_calendar_plan(self, state: CalendarReadState) -> dict[str, Any]:
        """执行受限计划，并保存可恢复、可展示的日历 Artifact。"""
        try:
            plan = CalendarReadPlan.model_validate(state["plan"])
        except (KeyError, ValidationError, ValueError) as error:
            return {
                "tool_context": "日历查询计划校验失败，无法安全调用外部日历。",
                "tool_result": ToolResultSummary(
                    status="failed",
                    tool_name="calendar.unspecified",
                    summary={"message": str(error)[:500]},
                ).model_dump(mode="json"),
            }
        if self._tool_execution is None:
            return {
                "tool_context": "当前日历工具尚未配置，无法读取实际日程。请明确说明不可用，不能编造日程。",
                "tool_result": ToolResultSummary(
                    status="failed",
                    tool_name=plan.tool_name,
                    summary={"message": "日历工具尚未配置"},
                ).model_dump(mode="json"),
            }

        run = _claimed_run_from_state(state)
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        execution = await self._tool_execution.execute_read_tool(
            run, context, plan.to_invocation()
        )
        decision = CalendarDecision.model_validate(state["calendar_decision"])
        display_plan = plan
        output_summary = execution.output_summary
        tool_context = execution.tool_context
        if decision.action == "find_free_time" and decision.time_mode == "explicit_interval" and execution.status == "succeeded":
            try:
                output_summary = explicit_interval_availability(decision, output_summary)
            except ValueError:
                return {"calendar_planning_error": "指定时段的日程数据无法核验，请稍后重试。"}
            display_plan = CalendarReadPlan(
                action="find_free_time", tool_name="calendar.find_free_time",
                arguments={**plan.arguments, "duration_minutes": decision.duration_minutes},
            )
            tool_context = "以下是指定时段确定性冲突检查结果：" + json.dumps(output_summary, ensure_ascii=False)
        summary = ToolResultSummary(
            status=execution.status,
            tool_name=execution.tool_name,
            summary=output_summary or {"message": "日历工具未返回可展示结果"},
        )
        artifact = build_calendar_read_artifact(display_plan, summary)
        artifacts = [artifact] if artifact is not None else []
        if artifacts and self._artifact_service is not None:
            await self._artifact_service.persist(run, artifacts)
        return {
            "tool_context": tool_context,
            "tool_result": summary.model_dump(mode="json"),
            "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
        }


def _claimed_run_from_state(state: CalendarReadState) -> ClaimedRun:
    """从图的稳定 ID 字段重建服务层所需的任务归属对象。"""
    return ClaimedRun(
        id=UUID(state["run_id"]),
        tenant_id=UUID(state["tenant_id"]),
        user_id=UUID(state["user_id"]),
        conversation_id=UUID(state["conversation_id"]),
    )
