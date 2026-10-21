"""程序产生委派身份；领域输出由专家边界核验后才能共享。"""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aegis_agent_worker.agent.contracts.decision import ResourceReference
from aegis_agent_worker.agent.contracts.output_validation import OutputValidationIssue
from .mail import MailMeetingRequirements


class DelegationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delegation_id: UUID
    sequence: int = Field(strict=True, ge=1)
    source: Literal["coordinator"] = "coordinator"
    target: str = Field(min_length=1, max_length=64)
    goal: str = Field(min_length=1, max_length=2000)


class ExpertResult(BaseModel):
    """安全摘要、稳定引用及已核验领域输出；不接收原始正文。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    delegation_id: UUID
    expert: str = Field(min_length=1, max_length=64)
    status: Literal["completed", "unavailable", "failed"]
    summary: str = Field(min_length=1, max_length=1200)
    references: tuple[ResourceReference, ...] = Field(default=(), max_length=10)
    draft: ResourceReference | None = None
    meeting_requirements: MailMeetingRequirements | None = None
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[A-Z][A-Z0-9_]+$")
    unresolved_fields: tuple[Literal["source_mail", "time_windows", "duration_minutes"], ...] = Field(default=(), max_length=3)
    output_stage: Literal["mail_meeting_planning", "mail_meeting_extraction", "calendar_team_planning", "calendar_team_draft"] | None = None
    validation_issues: tuple[OutputValidationIssue, ...] = Field(default=(), max_length=8)
    repair_count: int | None = Field(default=None, strict=True, ge=0)

    @model_validator(mode="after")
    def validate_artifacts(self):
        if len(set(self.references)) != len(self.references):
            raise ValueError("结果引用不能重复")
        if self.draft is not None:
            if (self.status != "completed" or self.draft.resource_type != "calendar_draft"
                    or self.draft.version is None or self.draft.source_version is not None):
                raise ValueError("最终草稿必须为带版本的已保存日历草稿引用")
        if self.meeting_requirements is not None and (
                self.expert != "mail" or self.status != "completed" or len(self.references) != 1
                or self.references[0].resource_type != "email" or not self.references[0].source_version):
            raise ValueError("邮件会议要求必须关联唯一真实邮件来源")
        if self.error_code and self.status == "completed":
            raise ValueError("成功结果不能携带失败代码")
        if self.unresolved_fields and self.status == "completed":
            raise ValueError("成功结果不能携带未解决项")
        if self.output_stage is not None or self.validation_issues or self.repair_count is not None:
            allowed = {"mail_meeting_planning": ("mail", "MODEL_OUTPUT_INVALID"),
                "mail_meeting_extraction": ("mail", "MODEL_OUTPUT_INVALID"),
                "calendar_team_planning": ("calendar", "CALENDAR_TEAM_OUTPUT_INVALID"),
                "calendar_team_draft": ("calendar", "CALENDAR_TEAM_DRAFT_INVALID")}
            if (allowed.get(self.output_stage) != (self.expert, self.error_code) or self.status == "completed"
                    or not self.validation_issues or self.repair_count is None):
                raise ValueError("输出诊断须对应专家、阶段、失败代码且完整提供")
        if len(set(self.unresolved_fields)) != len(self.unresolved_fields):
            raise ValueError("未解决项不能重复")
        return self
