"""日历页面响应：正式事件与站内草稿分开建模。"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class CalendarSourceVO(BaseModel):
    connection_id: UUID
    calendar_id: str = "primary"
    name: str = "主日历"
    provider: str
    source: str = "mock_calendar"
    timezone: str = "Asia/Shanghai"
    can_read: bool = True
    can_create_with_approval: bool


class CalendarEventVO(BaseModel):
    external_event_id: str
    title: str
    start_at: datetime
    end_at: datetime
    all_day: Literal[False] = False
    status: Literal["confirmed"] = "confirmed"
    attendee_count: int


class CalendarEventDetailVO(CalendarEventVO):
    connection_id: UUID
    calendar_id: str = "primary"
    source: str
    timezone: str
    description: str | None = None
    location: str | None = None
    attendees: list[str]
    origin_draft_id: UUID | None = None
    origin_run_id: UUID | None = None


class CalendarEventsVO(BaseModel):
    connection_id: UUID
    calendar_id: str = "primary"
    source: str
    timezone: str
    query_range: dict[str, datetime]
    fetched_at: datetime
    complete: Literal[True] = True
    events: list[CalendarEventVO]


class CalendarDraftVO(BaseModel):
    draft_id: UUID
    run_id: UUID
    connection_id: UUID
    calendar_id: str
    title: str
    start_at: datetime
    end_at: datetime
    version: int
    draft_status: str
    approval_item_id: UUID | None = None
    approval_status: str | None = None
    can_confirm: bool = False
    expires_at: datetime | None = None


class CalendarDraftDetailVO(CalendarDraftVO):
    description: str | None = None
    attendees: list[str] = Field(default_factory=list)
    timezone: str
    recommendation: dict[str, Any] | None = None
