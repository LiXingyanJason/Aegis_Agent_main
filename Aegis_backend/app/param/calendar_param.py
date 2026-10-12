"""日历页面查询参数；HTTP 时间必须显式携带时区。"""

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CalendarTimezoneParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str = "Asia/Shanghai"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """提前拒绝无法解析的 IANA 时区。"""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("无效的 IANA 时区") from error
        return value


class CalendarRangeParam(CalendarTimezoneParam):
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_range(self):
        """半开区间最多 62 天，禁止隐式猜测 HTTP 时间偏移。"""
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("start_at 和 end_at 必须携带时区")
        if not timedelta(0) < self.end_at - self.start_at <= timedelta(days=62):
            raise ValueError("查询范围必须大于零且不超过 62 天")
        return self


class CalendarEventsParam(CalendarRangeParam):
    connection_id: UUID
    calendar_id: Literal["primary"] = "primary"


class CalendarEventDetailParam(CalendarTimezoneParam):
    connection_id: UUID
    calendar_id: Literal["primary"] = "primary"
    external_event_id: str = Field(min_length=1, max_length=512)


class CalendarDraftsParam(CalendarRangeParam):
    connection_id: UUID | None = None
    approval_status: Literal["pending", "approved_executing", "executed", "rejected", "failed", "expired", "all"] = "pending"
