"""可委派邮件会议专家的局部State；正文和提取引文不在状态契约中。"""
from typing import Any, TypedDict


class MailMeetingState(TypedDict, total=False):
    run_id: str
    tenant_id: str
    user_id: str
    conversation_id: str | None
    run_type: str
    execution_policy_version: str
    delegation: dict[str, Any]
    team_context: dict[str, Any]
    expert_result: dict[str, Any]
    mail_meeting_task: str
    mail_meeting_round: int
    mail_meeting_searches: list[str]
    mail_meeting_empty_searches: int
    mail_meeting_candidates: list[dict[str, Any]]
    mail_meeting_matched_count: int
    mail_meeting_selected: dict[str, Any]
    mail_meeting_selection_locked: bool
    mail_meeting_read_reference: dict[str, Any]
    mail_meeting_source_hash: str
    mail_meeting_facts: dict[str, Any]
    mail_meeting_answers: list[str]
    mail_meeting_decision: dict[str, Any]
    mail_meeting_status: str
    input_request: dict[str, Any]
    input_request_id: str
    waiting_input: bool
