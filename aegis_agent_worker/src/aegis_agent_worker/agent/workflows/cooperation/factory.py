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

    def register(self,registry):
        registry.register('mail_calendar_meeting',FixedMeetingWorkflow(self.mail_agent,self.query_service,self.calendar_agent,
            self.confirmation_service,self.source_service,self.memory_context).build())
        registry.register_capability(ConversationCapability(domain='mail',intent='arrange_meeting_from_mail',
            workflow='mail_calendar_meeting',description='按一封明确邮件中的时间要求查询当前用户日历，生成会议草稿并等待用户确认；不是直接创建或发送。'))
