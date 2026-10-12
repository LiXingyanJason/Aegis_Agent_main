"""日历领域工作流的组装工厂。"""

from dataclasses import dataclass
from aegis_agent_worker.agent.contracts.decision import ConversationCapability

from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry
from aegis_agent_worker.agent.specialists.calendar_agent import CalendarAgent
from aegis_agent_worker.agent.workflows.calendar.confirmation_graph import CalendarConfirmationWorkflow
from aegis_agent_worker.agent.workflows.calendar.read_graph import CalendarReadWorkflow
from aegis_agent_worker.service.calendar.calendar_confirmation_service import CalendarConfirmationService
from aegis_agent_worker.service.runtime.conversation_artifact_service import ConversationArtifactService
from aegis_agent_worker.service.tool.tool_execution_service import ToolExecutionService


@dataclass(frozen=True, slots=True)
class CalendarWorkflowFactory:
    """仅由日历领域决定其子图依赖和注册名称。"""

    calendar_agent: CalendarAgent
    tool_execution: ToolExecutionService | None
    confirmation_service: CalendarConfirmationService | None
    artifact_service: ConversationArtifactService | None = None
    planning_audit: object | None = None

    def register(self, registry: GraphRegistry) -> None:
        """将日历读取和会议确认子图注册到根图可见的注册表。"""
        registry.register(
            "calendar_read",
            CalendarReadWorkflow(
                self.calendar_agent,
                self.tool_execution,
                self.artifact_service,
                self.planning_audit,
            ).build(),
        )
        registry.register(
            "calendar_confirmation",
            CalendarConfirmationWorkflow(
                self.calendar_agent, self.confirmation_service, self.planning_audit,
            ).build(),
        )
        if self.tool_execution is not None:
            for intent, description in (
                ("list_events", "查看指定范围日程"),
                ("find_free_time", "查询空闲时段"),
            ):
                registry.register_capability(ConversationCapability(
                    domain="calendar", intent=intent, workflow="calendar_read", description=description,
                ))
        if self.confirmation_service is not None:
            registry.register_capability(ConversationCapability(
                domain="calendar", intent="arrange_meeting", workflow="calendar_confirmation",
                description="生成会议草稿并等待人工确认；不能修改现有草稿",
            ))
