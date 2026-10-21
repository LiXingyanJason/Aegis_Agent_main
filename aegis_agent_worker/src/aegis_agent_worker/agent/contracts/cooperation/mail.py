"""邮件会议要求：领域事实、下一步动作与来源分离，无发送/日历工具动作。"""
import re
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, WithJsonSchema, field_validator, model_validator
from pydantic_core import PydanticCustomError
from typing import Annotated, Literal

from aegis_agent_worker.agent.contracts.decision import ResourceReference
from aegis_agent_worker.agent.contracts.mail import MailQueryPlan


ATTENDEE_EMAIL_PATTERN = r"^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,63}$"


def _normalize_attendee(value):
    normalized = value.strip().lower()
    if not re.fullmatch(ATTENDEE_EMAIL_PATTERN, normalized):
        raise PydanticCustomError("invalid_attendee_email", "参会人须为纯邮箱地址，不含姓名或括号")
    return normalized


AttendeeEmail = Annotated[str, AfterValidator(_normalize_attendee), WithJsonSchema({
    "type": "string", "pattern": ATTENDEE_EMAIL_PATTERN,
    "description": "纯邮箱地址，例如 person@example.com；不含姓名、尖括号或中文括号。",
})]


class MeetingTimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start_at: AwareDatetime
    end_at: AwareDatetime

    @model_validator(mode="after")
    def valid_range(self):
        if not timedelta(0) < self.end_at-self.start_at <= timedelta(days=62):
            raise ValueError("会议搜索范围必须大于零且不超过62天")
        return self


class MailMeetingFacts(BaseModel):
    """允许缺失，缺失时间/时长须补问；主题/参会人沿用可选语义。"""
    model_config = ConfigDict(extra="forbid", frozen=True)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    time_windows: tuple[MeetingTimeWindow, ...] = Field(default=(), max_length=5)
    duration_minutes: int | None = Field(default=None, strict=True, ge=15, le=480)
    attendees: tuple[AttendeeEmail, ...] = Field(default=(), max_length=10,
        description="仅提取明确受邀者的纯邮箱地址，大小写不敏感且不能重复；未指定时返回空数组。",
        json_schema_extra={"uniqueItems": True})

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("时区无效") from None
        return value

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value):
        if value is not None and not value.strip():
            raise ValueError("主题不能为空白")
        return value

    @field_validator("attendees")
    @classmethod
    def valid_addresses(cls, values):
        if len(set(values)) != len(values):
            raise PydanticCustomError("duplicate_attendee_email", "参会人邮箱重复；同一地址仅输出一次")
        return values

    @model_validator(mode="after")
    def valid_duration(self):
        if self.duration_minutes and any(
                item.end_at-item.start_at < timedelta(minutes=self.duration_minutes) for item in self.time_windows):
            raise ValueError("会议时长超过给定时间窗口")
        return self

    def missing_fields(self):
        return [name for name in ("time_windows", "duration_minutes") if not getattr(self, name)]


class MailMeetingRequirements(MailMeetingFacts):
    # 由Workflow按是否消费补充回答设置，不在模型的facts输出字段中。
    user_supplemented: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def require_time(self):
        if self.missing_fields():
            raise ValueError("返回会议要求前必须明确时间范围和时长")
        return self


class MailMeetingExtraction(BaseModel):
    """引文仅在模型调用节点核对，不存Checkpoint或团队上下文。"""
    model_config = ConfigDict(extra="forbid", frozen=True)
    facts: MailMeetingFacts
    evidence_quotes: tuple[str, ...] = Field(default=(), max_length=6)

    @field_validator("evidence_quotes")
    @classmethod
    def short_quotes(cls, values):
        if any(not value.strip() or len(value) > 160 for value in values):
            raise ValueError("事实引文必须为短文本")
        return values


class MailMeetingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra={"oneOf": [
        {"properties": {"action": {"const": action}, **{
            name: ({"not": {"type": "null"}} if name == expected else {"type": "null"})
            for name in ("query", "reference", "question")}},
         "required": ["action", *([expected] if expected else [])]}
        for action, expected in (("search", "query"), ("read_mail", "reference"), ("ask_user", "question"),
                                 ("return_requirements", None), ("finish_unavailable", None))
    ]})
    action: Literal["search", "read_mail", "ask_user", "return_requirements", "finish_unavailable"]
    query: MailQueryPlan | None = None
    reference: ResourceReference | None = None
    question: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def valid_action(self):
        expected = {"search": "query", "read_mail": "reference", "ask_user": "question"}.get(self.action)
        for name in ("query", "reference", "question"):
            if (getattr(self, name) is not None) != (name == expected):
                raise PydanticCustomError("action_parameter_mismatch", "动作与参数组合无效；仅提供当前动作所需参数")
        if self.query and self.query.action == "clarify":
            raise ValueError("补问须用ask_user")
        if self.reference and (self.reference.resource_type != "email" or not self.reference.source_version
                               or self.reference.version is not None):
            raise ValueError("读取必须使用带来源版本的邮件引用")
        if self.question and (not self.question.strip() or re.search(
                r"密码|密钥|验证码|token|secret|api.?key", self.question, re.I)):
            raise ValueError("不能索取凭据或提出空问题")
        return self
