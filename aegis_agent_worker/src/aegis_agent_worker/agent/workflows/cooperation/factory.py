"""固定协同工作流装配；不向模型开放任意子图注册。"""
from dataclasses import dataclass
from aegis_agent_worker.agent.contracts.decision import ConversationCapability
from aegis_agent_worker.agent.workflows.cooperation.fixed_meeting_workflow import FixedMeetingWorkflow


@dataclass(frozen=True)
class CooperationWorkflowFactory:
    mail_agent: object
    query_service: object
    calendar_agent: object
    confirmation_service: object
    source_service: object
    memory_context: object | None = None
    plan_enabled: bool = False
    planner: object | None = None

    def register(self,registry):
        if self.plan_enabled and self.planner is None:
            raise ValueError('协同规划已启用但未配置CooperationPlanner')
        from aegis_agent_worker.agent.routing.cooperation_registry import CooperationRegistry,FIXED_MEETING_STEPS
        def plan_registry():
            return CooperationRegistry(registry.cooperation_capabilities(),registry.names(),registry.capabilities())
        workflow=FixedMeetingWorkflow(self.mail_agent,self.query_service,self.calendar_agent,
            self.confirmation_service,self.source_service,self.memory_context,plan_registry if self.plan_enabled else None,
            planner=self.planner)
        registry.register('mail_calendar_meeting',workflow.build())
        registry.register_capability(ConversationCapability(domain='mail',intent='arrange_meeting_from_mail',
            workflow='mail_calendar_meeting',description='按一封明确邮件中的时间要求查询当前用户日历，生成会议草稿并等待用户确认；不是直接创建或发送。'))
        if self.plan_enabled:
            for capability in FIXED_MEETING_STEPS:
                registry.register(capability.workflow,workflow.build_step(capability.phase))
                registry.register_cooperation_capability(capability)
