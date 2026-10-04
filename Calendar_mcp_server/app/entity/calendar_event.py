"""日历领域实体。"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """来自任意日历提供商的一条标准化只读日程。"""

    title: str
    start_at: datetime
    end_at: datetime
    attendees: tuple[str, ...]
