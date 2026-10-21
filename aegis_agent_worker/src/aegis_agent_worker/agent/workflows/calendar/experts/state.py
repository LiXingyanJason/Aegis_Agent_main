"""日历专家私有状态；共享返回值仅为已核验ExpertResult。"""
from typing import Any, TypedDict


class CalendarExpertState(TypedDict, total=False):
    run_id: str
    tenant_id: str
    user_id: str
    conversation_id: str | None
    run_type: str
    execution_policy_version: str
    delegation: dict[str, Any]
    team_context: dict[str, Any]
    expert_result: dict[str, Any]
    calendar_task: str
    calendar_requirements: dict[str, Any]
    calendar_sources: list[dict[str, Any]]
    calendar_round: int
    calendar_observations: list[dict[str, Any]]
    calendar_connection_id: str
    calendar_answers: list[str]
    calendar_team_decision: dict[str, Any]
    calendar_expert_status: str
    calendar_last_issue: str
    input_request: dict[str, Any]
    input_request_id: str
    waiting_input: bool
