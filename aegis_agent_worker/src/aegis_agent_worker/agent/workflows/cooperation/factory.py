"""仅装配最终协同链路，根图只注册一个公开入口。"""
from dataclasses import dataclass
from aegis_agent_worker.agent.contracts.decision import ConversationCapability
from aegis_agent_worker.agent.routing.cooperation_registry import CooperationRegistry,MEETING_STEPS
from aegis_agent_worker.agent.routing.cooperation_profiles import MAIL_CALENDAR_PROFILES
from .steps.mail.requirements_workflow import MailRequirementsWorkflow
from .steps.calendar.candidates_workflow import CalendarCandidatesWorkflow
from .steps.calendar.meeting_confirmation_workflow import MeetingConfirmationWorkflow
from .cooperation_workflow import CooperationWorkflow
from .steps.mail.result_handlers import MAIL_RESULT_HANDLERS
from .steps.calendar.result_handlers import CALENDAR_RESULT_HANDLERS


COOPERATION_RESULT_HANDLERS={**MAIL_RESULT_HANDLERS,**CALENDAR_RESULT_HANDLERS}


@dataclass(frozen=True)
class CooperationWorkflowFactory:
    mail_agent: object
    query_service: object
    calendar_agent: object
    confirmation_service: object
    source_service: object
    planner: object
    memory_context: object | None = None

    def build_registry(self,steps):
        """在组合根装配声明和编译对象；新增生产者不改变通用调度图。"""
        return CooperationRegistry(MEETING_STEPS,steps,result_handlers=COOPERATION_RESULT_HANDLERS)

    def register(self,registry):
        if self.planner is None:
            raise ValueError('必须配置CooperationPlanner')
        def catalog():
            return cooperation_registry
        steps={
            'cooperation_mail_requirements':MailRequirementsWorkflow(self.mail_agent,self.query_service,
                self.calendar_agent,self.source_service,catalog).build(),
            'cooperation_calendar_candidates':CalendarCandidatesWorkflow(self.calendar_agent,
                self.confirmation_service,self.source_service,catalog,self.memory_context).build(),
            'cooperation_meeting_draft':MeetingConfirmationWorkflow(self.calendar_agent,
                self.confirmation_service,self.source_service,catalog,self.memory_context).build(),
        }
        cooperation_registry=self.build_registry(steps)
        registry.register('mail_calendar_meeting',CooperationWorkflow(self.planner,self.source_service,catalog).build())
        for profile in MAIL_CALENDAR_PROFILES:
            registry.register_capability(ConversationCapability(domain=profile.domain,intent=profile.intent,
                workflow='mail_calendar_meeting',description=profile.description))
