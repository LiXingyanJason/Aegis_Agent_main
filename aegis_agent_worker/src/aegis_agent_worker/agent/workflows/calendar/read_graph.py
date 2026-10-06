"""只读日历 LangGraph 子图：选择受控工具并执行查询。"""

from typing import Any
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from aegis_agent_worker.agent.specialists.scheduler_agent import SchedulerAgent
from aegis_agent_worker.agent.workflows.calendar.state import CalendarReadState
from aegis_agent_worker.config.database import TenantContext
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.service.tool_execution_service import ToolExecutionService
from aegis_agent_worker.tool.contracts import ToolInvocation


class CalendarReadWorkflow:
    """将日程专职 Agent 与受控工具执行服务组装成日历读取子图。"""

    def __init__(
        self,
        scheduler_agent: SchedulerAgent,
        tool_execution: ToolExecutionService | None,
    ) -> None:
        self._scheduler_agent = scheduler_agent
        self._tool_execution = tool_execution

    def build(self) -> Any:
        """构建可嵌入根图的日历只读子图。"""
        graph = StateGraph(CalendarReadState)
        graph.add_node("select_calendar_tool", self._select_calendar_tool)
        graph.add_node("invoke_calendar_tool", self._invoke_calendar_tool)
        graph.add_edge(START, "select_calendar_tool")
        graph.add_conditional_edges(
            "select_calendar_tool",
            self._route_after_selection,
            {"invoke_calendar_tool": "invoke_calendar_tool", "finish": END},
        )
        graph.add_edge("invoke_calendar_tool", END)
        return graph.compile()

    async def _select_calendar_tool(self, state: CalendarReadState) -> dict[str, Any]:
        """由日程专职 Agent 将用户表达转换为白名单 ToolInvocation。"""
        invocation = self._scheduler_agent.select_read_tool(
            state.get("latest_user_message", "")
        )
        if invocation is None:
            return {
                "tool_context": "未识别到可安全执行的日历读取请求，请基于现有会话正常回复。"
            }
        return {
            "selected_tool_name": invocation.tool_name,
            "selected_tool_arguments": invocation.arguments,
        }

    @staticmethod
    def _route_after_selection(state: CalendarReadState) -> str:
        """只有得到受控工具调用时才进入外部服务调用节点。"""
        return "invoke_calendar_tool" if state.get("selected_tool_name") else "finish"

    async def _invoke_calendar_tool(self, state: CalendarReadState) -> dict[str, str]:
        """调用应用服务，由其处理审计、事务、事件与 MCP 网络请求。"""
        if self._tool_execution is None:
            return {
                "tool_context": "当前日历工具尚未配置，无法读取实际日程。请明确说明不可用，不能编造日程。"
            }
        run = _claimed_run_from_state(state)
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        invocation = ToolInvocation(
            tool_name=state["selected_tool_name"],
            arguments=state["selected_tool_arguments"],
        )
        tool_context = await self._tool_execution.invoke_read_tool(run, context, invocation)
        return {"tool_context": tool_context}


def _claimed_run_from_state(state: CalendarReadState) -> ClaimedRun:
    """从图的稳定 ID 字段重建服务层所需的任务归属对象。"""
    return ClaimedRun(
        id=UUID(state["run_id"]),
        tenant_id=UUID(state["tenant_id"]),
        user_id=UUID(state["user_id"]),
        conversation_id=UUID(state["conversation_id"]),
    )
