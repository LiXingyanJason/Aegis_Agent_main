"""计划中的输出契约标签对应真实Schema，不是仅供模型看的自由字符串。"""
from types import MappingProxyType
from typing import Literal
from pydantic import model_validator
from .cooperation import TaskStepResult


class MailRequirementsV1(TaskStepResult):
    step_id: Literal['mail_requirements']

    @model_validator(mode='after')
    def mail_fields_only(self):
        if self.candidate_ids or self.candidate_version or self.draft_id or self.approval_item_id:
            raise ValueError('邮件结果不能伪造候选或审批引用')
        return self


class CalendarCandidatesV1(TaskStepResult):
    step_id: Literal['calendar_candidates']

    @model_validator(mode='after')
    def candidate_fields_only(self):
        if self.source_digest or self.calendar_decision or self.draft_id or self.approval_item_id:
            raise ValueError('候选结果不能覆盖邮件计划或生成审批')
        return self


class MeetingDraftV1(TaskStepResult):
    step_id: Literal['meeting_draft']

    @model_validator(mode='after')
    def draft_fields_only(self):
        if self.source_digest or self.calendar_decision or self.candidate_ids or self.candidate_version:
            raise ValueError('草稿结果不能覆盖来源或候选快照')
        return self


OUTPUT_CONTRACTS=MappingProxyType({model.__name__:model for model in (MailRequirementsV1,CalendarCandidatesV1,MeetingDraftV1)})
