"""复用既有日历确认业务节点的协同步骤基类。"""
from aegis_agent_worker.agent.workflows.calendar.confirmation_graph import CalendarConfirmationWorkflow


class CalendarStepWorkflow(CalendarConfirmationWorkflow):
    def __init__(self,calendar_agent,confirmation_service,source_service,registry_provider,memory_context=None):
        super().__init__(calendar_agent,confirmation_service,memory_context=memory_context)
        self._source_service=source_service
        self._registry=registry_provider
