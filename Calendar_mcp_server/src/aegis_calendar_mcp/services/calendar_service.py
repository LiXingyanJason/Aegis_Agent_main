"""日历查询和空闲时间计算。"""

from datetime import UTC, datetime, time, timedelta

from aegis_calendar_mcp.providers.base import CalendarProvider
from aegis_calendar_mcp.schemas.calendar import CalendarEventVO, CalendarEventsVO, CalendarRequestContext, FindFreeTimeParam, FreeTimeSlot, FreeTimeSlotVO, FreeTimeVO, ListEventsParam


class CalendarService:
    """实现 Provider 无关的日历业务规则。"""

    def __init__(self, provider: CalendarProvider) -> None:
        self._provider = provider

    async def list_events(self, context: CalendarRequestContext, param: ListEventsParam) -> CalendarEventsVO:
        events = await self._provider.list_events(context, param.start_at, param.end_at)
        return CalendarEventsVO(query_range={"start_at": param.start_at, "end_at": param.end_at}, events=[CalendarEventVO.from_entity(event) for event in events])

    async def find_free_time(self, context: CalendarRequestContext, param: FindFreeTimeParam) -> FreeTimeVO:
        busy_events = await self._provider.list_events(context, param.start_at, param.end_at)
        slots: list[FreeTimeSlot] = []
        day, start_at, end_at = param.start_at.astimezone(UTC).date(), param.start_at.astimezone(UTC), param.end_at.astimezone(UTC)
        while datetime.combine(day, time.min, tzinfo=UTC) < end_at:
            work_start = max(datetime.combine(day, time(1, 0), tzinfo=UTC), start_at)
            work_end = min(datetime.combine(day, time(10, 0), tzinfo=UTC), end_at)
            cursor = work_start
            busy_for_day = sorted(((max(event.start_at, work_start), min(event.end_at, work_end)) for event in busy_events if event.start_at < work_end and event.end_at > work_start), key=lambda item: item[0])
            for busy_start, busy_end in busy_for_day:
                self._append_slot(slots, cursor, busy_start, param.duration_minutes)
                cursor = max(cursor, busy_end)
            self._append_slot(slots, cursor, work_end, param.duration_minutes)
            day += timedelta(days=1)
        return FreeTimeVO(query_range={"start_at": param.start_at, "end_at": param.end_at}, duration_minutes=param.duration_minutes, participants=param.participants, available_slots=[FreeTimeSlotVO.from_entity(slot) for slot in slots[:10]])

    @staticmethod
    def _append_slot(slots: list[FreeTimeSlot], start_at: datetime, end_at: datetime, duration_minutes: int) -> None:
        if end_at - start_at >= timedelta(minutes=duration_minutes):
            slots.append(FreeTimeSlot(start_at=start_at, end_at=end_at))
