"""日历 Provider 的最小接口。"""

from datetime import datetime
from typing import Protocol

from aegis_calendar_mcp.schemas.calendar import CalendarEvent, CalendarRequestContext


class CalendarProvider(Protocol):
    async def list_events(self, context: CalendarRequestContext, start_at: datetime, end_at: datetime) -> list[CalendarEvent]:
        """读取给定时间范围内有重叠的日程。"""

    async def create_event(self, context: CalendarRequestContext, event: CalendarEvent, idempotency_key: str) -> str:
        """创建事件并返回外部事件标识；相同幂等键不得重复创建。"""
