"""开发联调用的固定日历 Provider。"""

from datetime import UTC, datetime

from aegis_calendar_mcp.schemas.calendar import CalendarEvent, CalendarRequestContext


class MockCalendarProvider:
    """不持久化用户数据的固定开发数据源。"""

    _events = (
        CalendarEvent("项目同步", datetime(2026, 10, 8, 7, 0, tzinfo=UTC), datetime(2026, 10, 8, 7, 30, tzinfo=UTC), ("jason@example.com", "wangmin@example.com")),
        CalendarEvent("产品评审", datetime(2026, 10, 9, 9, 0, tzinfo=UTC), datetime(2026, 10, 9, 10, 0, tzinfo=UTC), ("jason@example.com",)),
        CalendarEvent("周报整理", datetime(2026, 10, 10, 2, 0, tzinfo=UTC), datetime(2026, 10, 10, 3, 0, tzinfo=UTC), ("jason@example.com",)),
    )

    async def list_events(self, context: CalendarRequestContext, start_at: datetime, end_at: datetime) -> list[CalendarEvent]:
        """返回范围重叠的固定事件；真实 Provider 会利用上下文取凭据。"""
        return [event for event in self._events if event.start_at < end_at and event.end_at > start_at]
