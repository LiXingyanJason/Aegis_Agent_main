"""开发联调用的固定日历 Provider。"""

from datetime import UTC, datetime

from app.entity.calendar_event import CalendarEvent
from app.security.aegis_context import AegisContext


class MockCalendarProvider:
    """不访问数据库的固定数据源，后续可被真实日历 Provider 替换。"""

    _events = (
        CalendarEvent("项目同步", datetime(2026, 10, 5, 7, 0, tzinfo=UTC), datetime(2026, 10, 5, 7, 30, tzinfo=UTC), ("jason@example.com", "wangmin@example.com")),
        CalendarEvent("产品评审", datetime(2026, 10, 5, 9, 0, tzinfo=UTC), datetime(2026, 10, 5, 10, 0, tzinfo=UTC), ("jason@example.com",)),
        CalendarEvent("周报整理", datetime(2026, 10, 6, 2, 0, tzinfo=UTC), datetime(2026, 10, 6, 3, 0, tzinfo=UTC), ("jason@example.com",)),
    )

    async def list_events(
        self, context: AegisContext, start_at: datetime, end_at: datetime
    ) -> list[CalendarEvent]:
        """返回与查询范围重叠的固定日程；context 留给真实 Provider 鉴权使用。"""
        return [event for event in self._events if event.start_at < end_at and event.end_at > start_at]
