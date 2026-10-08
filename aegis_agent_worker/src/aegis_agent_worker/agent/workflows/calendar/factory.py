"""日历领域工作流的组装工厂。"""

from dataclasses import dataclass

from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry
from aegis_agent_worker.agent.specialists.calendar_agent import CalendarAgent
from aegis_agent_worker.agent.workflows.calendar.confirmation_graph import CalendarConfirmationWorkflow
from aegis_agent_worker.agent.workflows.calendar.read_graph import CalendarReadWorkflow
from aegis_agent_worker.service.calendar.calendar_confirmation_service import CalendarConfirmationService
from aegis_agent_worker.service.tool.tool_execution_service import ToolExecutionService


@dataclass(frozen=True, slots=True)
class CalendarWorkflowFactory:
    """仅由日历领域决定其子图依赖和注册名称。"""

    calendar_agent: CalendarAgent
    tool_execution: ToolExecutionService | None
    confirmation_service: CalendarConfirmationService | None

    def register(self, registry: GraphRegistry) -> None:
        """将日历读取和会议确认子图注册到根图可见的注册表。"""
        registry.register(
            "calendar_read",
            CalendarReadWorkflow(self.calendar_agent, self.tool_execution).build(),
        )
        registry.register(
            "calendar_confirmation",
            CalendarConfirmationWorkflow(
                self.calendar_agent, self.confirmation_service
            ).build(),
        )
