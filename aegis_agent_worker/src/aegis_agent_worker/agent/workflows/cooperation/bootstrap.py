"""生产团队装配；复用现有专家、Gateway、输入服务及同一个GuardedLLMClient。"""
from dataclasses import dataclass

from aegis_agent_worker.agent.routing.team_registry import TeamRegistry
from aegis_agent_worker.agent.specialists.cooperation_agent import CooperationAgent
from aegis_agent_worker.agent.workflows.calendar.approval_workflow import CalendarApprovalWorkflow
from aegis_agent_worker.agent.workflows.calendar.experts.meeting_draft_workflow import CalendarMeetingDraftWorkflow
from aegis_agent_worker.agent.workflows.mail.experts.meeting_requirements_workflow import MailMeetingRequirementsWorkflow
from aegis_agent_worker.agent.workflows.cooperation.factory import CooperationWorkflowFactory
from aegis_agent_worker.service.calendar.calendar_draft_service import CalendarDraftService
from aegis_agent_worker.service.calendar.calendar_expert_service import CalendarExpertService
from aegis_agent_worker.service.mail.mail_meeting_source_service import MailMeetingSourceService
from aegis_agent_worker.service.cooperation.team_event_service import TeamEventService


@dataclass(frozen=True)
class CooperationRuntime:
    factory: CooperationWorkflowFactory
    approval_readiness: CalendarDraftService


def create_cooperation_runtime(*, database, runs, events, llm_client, mail_agent, calendar_agent,
        mail_queries, mail_reader, calendar_queries, calendar_confirmation, input_requests,
        memory_context, policy_provider):
    profile = policy_provider.current()
    if not all((profile.cooperation.enabled, profile.mail_meeting.enabled, profile.calendar_team.enabled)):
        raise ValueError("完整团队入口需要同时启用协调、邮件和日历策略")
    sources = MailMeetingSourceService(database, mail_queries, mail_reader)
    drafts = CalendarDraftService(database, runs, sources, events)
    mail = MailMeetingRequirementsWorkflow(mail_agent, sources, input_requests, policy_provider)
    calendar = CalendarMeetingDraftWorkflow(calendar_agent, CalendarExpertService(calendar_queries),
        drafts, input_requests, memory_context, policy_provider)
    factory = CooperationWorkflowFactory(CooperationAgent(llm_client, policy_provider),
        TeamRegistry((mail.binding(), calendar.binding())), TeamEventService(database, runs, events),
        CalendarApprovalWorkflow(calendar_confirmation, drafts).build(), policy_provider)
    return CooperationRuntime(factory, drafts)
