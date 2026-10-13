"""所有默认工具的严格参数契约，身份和连接不能由参数传入。"""
from typing import Literal
from datetime import timedelta
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, model_validator,field_validator
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CalendarArgs(EmptyArgs):
    start_at: AwareDatetime
    end_at: AwareDatetime
    timezone: str | None = Field(default=None,max_length=64)
    requested_date: str | None = Field(default=None,max_length=32)
    @field_validator("timezone")
    @classmethod
    def valid_zone(cls,value):
        if value is not None:
            try: ZoneInfo(value)
            except (ZoneInfoNotFoundError,ValueError) as error: raise ValueError("时区无效") from error
        return value
    @model_validator(mode="after")
    def validate_range(self):
        if not timedelta(0)<self.end_at-self.start_at<=timedelta(days=62):
            raise ValueError("时间范围无效")
        return self


class AvailabilityArgs(CalendarArgs):
    duration_minutes: int = Field(default=30, strict=True, ge=15, le=480)
    participants: list[str] = Field(default_factory=list, max_length=10)


class CalendarWriteArgs(CalendarArgs):
    title: str = Field(min_length=1,max_length=500)
    description: str | None = Field(default=None,max_length=4000)
    attendees: list[str] = Field(default_factory=list,max_length=20)
    idempotency_key: str = Field(min_length=1,max_length=255)


class MailGetArgs(EmptyArgs):
    provider_message_id: str = Field(min_length=1,max_length=512)


class MailSendArgs(EmptyArgs):
    to: list[str] = Field(min_length=1,max_length=20)
    cc: list[str] = Field(default_factory=list,max_length=20)
    subject: str = Field(min_length=1,max_length=998)
    body: str = Field(min_length=1,max_length=65535)
    idempotency_key: str = Field(min_length=1,max_length=128)
    @model_validator(mode="after")
    def addresses(self):
        import re
        if not all(re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,63}", item) for item in self.to+self.cc):
            raise ValueError("收件人邮箱无效")
        return self


SCHEMAS = {
    "calendar.list_events": CalendarArgs, "calendar.find_free_time": AvailabilityArgs,
    "calendar.create_event": CalendarWriteArgs, "mail.messages.list": EmptyArgs,
    "mail.messages.get": MailGetArgs, "mail.messages.send": MailSendArgs,
}
