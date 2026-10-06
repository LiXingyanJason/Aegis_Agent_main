"""日历工具的输入、输出和领域模型。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


@dataclass(frozen=True, slots=True)
class CalendarRequestContext:
    """主后端在 MCP `_meta` 中传入的无状态调用上下文。"""

    connection_id: UUID
    tenant_id: UUID
    user_id: UUID
    run_id: UUID


def parse_request_context(meta: dict[str, Any] | None) -> CalendarRequestContext:
    """仅校验调用上下文格式；身份、租户和权限由主后端负责。"""
    if not isinstance(meta, dict):
        raise ValueError("缺少有效的 Aegis 调用上下文")
    try:
        return CalendarRequestContext(
            connection_id=UUID(str(meta["aegis_connection_id"])),
            tenant_id=UUID(str(meta["aegis_tenant_id"])),
            user_id=UUID(str(meta["aegis_user_id"])),
            run_id=UUID(str(meta["aegis_run_id"])),
        )
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("缺少有效的 Aegis 连接与身份上下文") from error


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """Provider 返回的一条标准化日程。"""

    title: str
    start_at: datetime
    end_at: datetime
    attendees: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FreeTimeSlot:
    """满足会议时长的一段空闲时间。"""

    start_at: datetime
    end_at: datetime


class TimeRangeParam(BaseModel):
    """日历查询共用的带时区时间范围。"""

    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_range(self):
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("start_at 和 end_at 必须携带时区")
        if self.end_at <= self.start_at:
            raise ValueError("end_at 必须晚于 start_at")
        return self


class ListEventsParam(TimeRangeParam):
    """calendar.list_events 的业务参数。"""


class FindFreeTimeParam(TimeRangeParam):
    """calendar.find_free_time 的业务参数。"""

    duration_minutes: int = Field(default=30, ge=15, le=480)
    participants: list[str] = Field(default_factory=list, max_length=10)


class CreateEventParam(TimeRangeParam):
    """calendar.create_event 的受控写入参数。"""

    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=4000)
    attendees: list[str] = Field(default_factory=list, max_length=20)
    idempotency_key: str = Field(min_length=1, max_length=255)


class CreateEventVO(BaseModel):
    """创建日历事件后的最小安全返回。"""

    source: str = "mock_calendar"
    external_event_id: str


class CalendarEventVO(BaseModel):
    title: str
    start_at: datetime
    end_at: datetime
    attendees: list[str]

    @classmethod
    def from_entity(cls, event: CalendarEvent) -> "CalendarEventVO":
        return cls(title=event.title, start_at=event.start_at, end_at=event.end_at, attendees=list(event.attendees))


class CalendarEventsVO(BaseModel):
    source: str = "mock_calendar"
    query_range: dict[str, datetime]
    events: list[CalendarEventVO]


class FreeTimeSlotVO(BaseModel):
    start_at: datetime
    end_at: datetime

    @classmethod
    def from_entity(cls, slot: FreeTimeSlot) -> "FreeTimeSlotVO":
        return cls(start_at=slot.start_at, end_at=slot.end_at)


class FreeTimeVO(BaseModel):
    source: str = "mock_calendar"
    query_range: dict[str, datetime]
    duration_minutes: int
    participants: list[str]
    participant_resolution: str = "mock_current_user_only"
    available_slots: list[FreeTimeSlotVO]
