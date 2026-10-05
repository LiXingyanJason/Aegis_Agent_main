"""日历 Provider 的最小接口。"""

from datetime import datetime
from typing import Protocol

from aegis_calendar_mcp.schemas.calendar import CalendarEvent, CalendarRequestContext


class CalendarProvider(Protocol):
    async def list_events(self, context: CalendarRequestContext, start_at: datetime, end_at: datetime) -> list[CalendarEvent]:
        """读取给定时间范围内有重叠的日程。"""
