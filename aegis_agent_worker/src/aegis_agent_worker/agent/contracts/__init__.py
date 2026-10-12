"""Agent 图之间传递的受控结构化契约。"""

from aegis_agent_worker.agent.contracts.calendar import (
    CalendarFreeTimeCandidate,
    CalendarMeetingDraftProposal,
    CalendarReadPlan,
    CalendarSlotRecommendation,
    build_calendar_read_artifact,
    extract_calendar_free_time_candidates,
)
from aegis_agent_worker.agent.contracts.common import ConversationArtifact, ToolResultSummary

__all__ = [
    "CalendarReadPlan",
    "CalendarFreeTimeCandidate",
    "CalendarMeetingDraftProposal",
    "CalendarSlotRecommendation",
    "ConversationArtifact",
    "ToolResultSummary",
    "build_calendar_read_artifact",
    "extract_calendar_free_time_candidates",
]

