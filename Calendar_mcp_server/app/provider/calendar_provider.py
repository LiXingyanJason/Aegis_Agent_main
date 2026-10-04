"""日历提供商适配接口。"""

from datetime import datetime
from typing import Protocol

from app.entity.calendar_event import CalendarEvent
from app.security.aegis_context import AegisContext


class CalendarProvider(Protocol):
    """Google、Outlook、飞书和 Mock 实现均应遵守的最小读取接口。"""

    async def list_events(
        self, context: AegisContext, start_at: datetime, end_at: datetime
    ) -> list[CalendarEvent]:
        """读取调用用户在时间范围内有重叠的日程。"""
