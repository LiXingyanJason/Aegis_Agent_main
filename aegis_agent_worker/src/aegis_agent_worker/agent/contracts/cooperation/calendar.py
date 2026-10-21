"""日历专家只选择受控查询、补问和站内草稿动作，不能批准写入。"""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from .mail import MailMeetingRequirements
from aegis_agent_worker.agent.contracts.calendar import CalendarFreeTimeCandidate, CalendarMeetingDraftProposal
from datetime import datetime, timedelta


class CalendarTeamDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra={"oneOf": [
        {"properties": {"action": {"const": action}, **{
            name: ({"not": {"type": "null"}} if name == expected else
                   ({} if action == "ask_user" and name == "proposed_requirements" else {"type": "null"}))
            for name in ("window_index", "question", "proposed_requirements")}},
         "required": ["action", *([expected] if expected else [])]}
        for action, expected in (("query_availability", "window_index"), ("ask_user", "question"),
                                 ("generate_draft", None), ("finish_unavailable", None))
    ]})
    action: Literal["query_availability", "ask_user", "generate_draft", "finish_unavailable"]
    window_index: int | None = Field(default=None, strict=True, ge=0, le=4)
    question: str | None = Field(default=None, min_length=1, max_length=500)
    proposed_requirements: MailMeetingRequirements | None = None

    @model_validator(mode="after")
    def valid_action(self):
        if (self.window_index is not None) != (self.action == "query_availability"):
            raise PydanticCustomError("action_parameter_mismatch", "只有query_availability需要window_index")
        if (self.question is not None) != (self.action == "ask_user"):
            raise PydanticCustomError("action_parameter_mismatch", "只有ask_user需要question")
        if self.proposed_requirements is not None and self.action != "ask_user":
            raise PydanticCustomError("action_parameter_mismatch", "只有ask_user可以携带proposed_requirements")
        if self.question and (not self.question.strip() or re.search(
                r"密码|密钥|验证码|token|secret|api.?key", self.question, re.I)):
            raise ValueError("补问不能索取凭据")
        return self


def validate_team_proposal(requirements, proposal, candidates):
    """候选/时长/明确主题和邮箱均由代码校验，不依赖模型自报已遵守。"""
    requirements = MailMeetingRequirements.model_validate(requirements)
    proposal = CalendarMeetingDraftProposal.model_validate(proposal)
    by_id = {item.slot_id: item for item in candidates}
    chosen = [proposal.selected_slot_id, *proposal.alternative_slot_ids]
    if any(key not in by_id for key in chosen):
        raise ValueError("草稿选择了不存在的候选")
    if set(proposal.attendees) != set(requirements.attendees):
        raise ValueError("草稿改变了明确参会人")
    if requirements.title is not None and proposal.title != requirements.title:
        raise ValueError("草稿改变了明确主题")
    for key in chosen:
        slot = by_id[key]
        start, end = datetime.fromisoformat(slot.start_at), datetime.fromisoformat(slot.end_at)
        if start.tzinfo is None or end.tzinfo is None or end-start < timedelta(minutes=requirements.duration_minutes):
            raise ValueError("候选时间或时长无效")
        if not any(window.start_at <= start and end <= window.end_at for window in requirements.time_windows):
            raise ValueError("候选越出明确时间窗口")
    return proposal, by_id
