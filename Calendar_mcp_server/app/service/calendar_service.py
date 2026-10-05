"""只读日历查询与空闲时间计算的业务服务。"""

from datetime import UTC, datetime, time, timedelta

from app.entity.free_time_slot import FreeTimeSlot
from app.param.calendar_param import FindFreeTimeParam, ListEventsParam
from app.provider.calendar_provider import CalendarProvider
from app.security.aegis_context import AegisContext
from app.vo.calendar_vo import CalendarEventsVO, CalendarEventVO, FreeTimeSlotVO, FreeTimeVO


class CalendarService:
    """隔离 MCP 协议层与具体日历提供商，实现日历领域业务规则。"""

    def __init__(self, provider: CalendarProvider) -> None:
        self._provider = provider

    async def list_events(self, context: AegisContext, param: ListEventsParam) -> CalendarEventsVO:
        """查询日程并转换为稳定的 MCP 输出模型。"""
        events = await self._provider.list_events(context, param.start_at, param.end_at)
        return CalendarEventsVO(
            query_range={"start_at": param.start_at, "end_at": param.end_at},
            events=[CalendarEventVO.from_entity(event) for event in events],
        )

    async def find_free_time(self, context: AegisContext, param: FindFreeTimeParam) -> FreeTimeVO:
        """
        在 UTC 工作时段中排除忙碌事件，计算满足时长要求的空闲时间。

        """
        busy_events = await self._provider.list_events(context, param.start_at, param.end_at)
        slots: list[FreeTimeSlot] = [] # 最终找到的空闲时间段
        day = param.start_at.astimezone(UTC).date()
        start_at = param.start_at.astimezone(UTC)
        end_at = param.end_at.astimezone(UTC)
        while datetime.combine(day, time.min, tzinfo=UTC) < end_at: # 逐天处理
            # 定义当天可安排会议的工作时间 01:00–10:00 UTC == 09:00–18:00 Asia/Shanghai
            work_start = max(datetime.combine(day, time(1, 0), tzinfo=UTC), start_at)
            work_end = min(datetime.combine(day, time(10, 0), tzinfo=UTC), end_at)
            cursor = work_start # 当前扫描到的位置
            busy_for_day = sorted( # 筛选并排序当天工作时间内的忙碌日程
                ((max(event.start_at, work_start), min(event.end_at, work_end)) for event in busy_events if event.start_at < work_end and event.end_at > work_start),
                key=lambda item: item[0],
            )# 最后按开始时间排序，确保后面的扫描按时间顺序进行
            for busy_start, busy_end in busy_for_day:
                self._append_slot(slots, cursor, busy_start, param.duration_minutes) # 检查该候选时间是否至少有用户要求的时长
                cursor = max(cursor, busy_end)
            self._append_slot(slots, cursor, work_end, param.duration_minutes)
            day += timedelta(days=1)
        return FreeTimeVO(
            query_range={"start_at": param.start_at, "end_at": param.end_at},
            duration_minutes=param.duration_minutes,
            participants=param.participants,
            available_slots=[FreeTimeSlotVO.from_entity(slot) for slot in slots[:10]],
        )

    @staticmethod
    def _append_slot(slots: list[FreeTimeSlot], start_at: datetime, end_at: datetime, duration: int) -> None:
        """仅保留足够长的空闲段。"""
        if end_at - start_at >= timedelta(minutes=duration):
            slots.append(FreeTimeSlot(start_at=start_at, end_at=end_at))
