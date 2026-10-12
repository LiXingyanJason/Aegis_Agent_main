"""Agent 图之间传递的受控结构化契约。"""

from aegis_agent_worker.agent.contracts.calendar import (
    CalendarFreeTimeCandidate,
    CalendarDecision,
    CalendarMeetingDraftProposal,
    CalendarReadPlan,
    CalendarSlotRecommendation,
    build_calendar_read_artifact,
    extract_calendar_free_time_candidates,
)
from aegis_agent_worker.agent.contracts.common import ConversationArtifact, ToolResultSummary
from aegis_agent_worker.agent.contracts.decision import MainDecision, AgentHandoff, ResourceReference
from aegis_agent_worker.agent.contracts.mail import MailQueryPlan, MailDecision

__all__ = [
    "CalendarReadPlan",
    "CalendarDecision",
    "MainDecision",
    "MailQueryPlan",
    "MailDecision",
    "AgentHandoff",
    "ResourceReference",
    "CalendarFreeTimeCandidate",
    "CalendarMeetingDraftProposal",
    "CalendarSlotRecommendation",
    "ConversationArtifact",
    "ToolResultSummary",
    "build_calendar_read_artifact",
    "extract_calendar_free_time_candidates",
]

