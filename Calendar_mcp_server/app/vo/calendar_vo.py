"""日历工具的 MCP structuredContent 输出模型。"""

from datetime import datetime

from pydantic import BaseModel

from app.entity.calendar_event import CalendarEvent
from app.entity.free_time_slot import FreeTimeSlot


class CalendarEventVO(BaseModel):
    """对外返回的一条日程。"""

    title: str
    start_at: datetime
    end_at: datetime
    attendees: list[str]

    @classmethod
    def from_entity(cls, event: CalendarEvent) -> "CalendarEventVO":
        """将领域实体转换为 MCP 输出对象。"""
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
        """将空闲时间实体转换为 MCP 输出对象。"""
        return cls(start_at=slot.start_at, end_at=slot.end_at)


class FreeTimeVO(BaseModel):
    source: str = "mock_calendar"
    query_range: dict[str, datetime]
    duration_minutes: int
    participants: list[str]
    participant_resolution: str = "mock_current_user_only"
    available_slots: list[FreeTimeSlotVO]
