"""日历工具参数模型与时间范围校验。"""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class TimeRangeParam(BaseModel):
    """两个日历工具共用的带时区时间范围。"""

    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_range(self):
        """拒绝反向、相等或不带时区的时间范围。"""
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("start_at 和 end_at 必须携带时区")
        if self.end_at <= self.start_at:
            raise ValueError("end_at 必须晚于 start_at")
        return self


class ListEventsParam(TimeRangeParam):
    """calendar.list_events 参数。"""


class FindFreeTimeParam(TimeRangeParam):
    """calendar.find_free_time 参数。"""

    duration_minutes: int = Field(default=30, ge=15, le=480)
    participants: list[str] = Field(default_factory=list, max_length=10)
