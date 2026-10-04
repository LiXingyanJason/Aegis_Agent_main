"""开发期固定日程数据和只读查询计算。"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta


@dataclass(frozen=True, slots=True)
class MockEvent:
    """一条仅用于本地联调的日历事件。"""

    title: str
    start_at: datetime
    end_at: datetime
    attendees: tuple[str, ...]


class MockCalendarService:
    """不依赖数据库的固定日程数据源，后续可替换为 Google/Outlook Adapter。"""

    _events = (
        MockEvent(
            title="项目同步",
            start_at=datetime(2026, 10, 5, 7, 0, tzinfo=UTC),
            end_at=datetime(2026, 10, 5, 7, 30, tzinfo=UTC),
            attendees=("jason@example.com", "wangmin@example.com"),
        ),
        MockEvent(
            title="产品评审",
            start_at=datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
            end_at=datetime(2026, 10, 5, 10, 0, tzinfo=UTC),
            attendees=("jason@example.com",),
        ),
        MockEvent(
            title="周报整理",
            start_at=datetime(2026, 10, 6, 2, 0, tzinfo=UTC),
            end_at=datetime(2026, 10, 6, 3, 0, tzinfo=UTC),
            attendees=("jason@example.com",),
        ),
    )

    def list_events(self, start_at: datetime, end_at: datetime) -> dict:
        """返回与查询范围有时间重叠的固定日程。"""
        events = [
            {
                "title": event.title,
                "start_at": event.start_at.isoformat(),
                "end_at": event.end_at.isoformat(),
                "attendees": list(event.attendees),
            }
            for event in self._events
            if event.start_at < end_at and event.end_at > start_at
        ]
        return {
            "source": "mock_calendar",
            "query_range": {"start_at": start_at.isoformat(), "end_at": end_at.isoformat()},
            "events": events,
        }

    def find_free_time(
        self, start_at: datetime, end_at: datetime, duration_minutes: int, participants: list[str]
    ) -> dict:
        """在工作时段内排除固定忙碌事件，返回可容纳所需时长的空闲段。"""
        busy_events = [
            event
            for event in self._events
            if event.start_at < end_at and event.end_at > start_at
        ]
        slots: list[dict[str, str]] = []
        day = start_at.date()
        while datetime.combine(day, time.min, tzinfo=UTC) < end_at:
            work_start = max(datetime.combine(day, time(1, 0), tzinfo=UTC), start_at)
            work_end = min(datetime.combine(day, time(10, 0), tzinfo=UTC), end_at)
            cursor = work_start
            busy_for_day = sorted(
                (
                    (max(event.start_at, work_start), min(event.end_at, work_end))
                    for event in busy_events
                    if event.start_at < work_end and event.end_at > work_start
                ),
                key=lambda item: item[0],
            )
            for busy_start, busy_end in busy_for_day:
                self._append_slot(slots, cursor, busy_start, duration_minutes)
                cursor = max(cursor, busy_end)
            self._append_slot(slots, cursor, work_end, duration_minutes)
            day += timedelta(days=1)
        return {
            "source": "mock_calendar",
            "query_range": {"start_at": start_at.isoformat(), "end_at": end_at.isoformat()},
            "duration_minutes": duration_minutes,
            "participants": participants,
            "participant_resolution": "mock_current_user_only",
            "available_slots": slots[:10],
        }

    @staticmethod
    def _append_slot(slots: list[dict[str, str]], start_at: datetime, end_at: datetime, duration: int) -> None:
        """仅保留长度足够的空闲区间。"""
        if end_at - start_at >= timedelta(minutes=duration):
            slots.append({"start_at": start_at.isoformat(), "end_at": end_at.isoformat()})
