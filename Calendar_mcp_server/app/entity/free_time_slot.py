"""空闲时间领域实体。"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class FreeTimeSlot:
    """满足会议时长要求的一段空闲时间。"""

    start_at: datetime
    end_at: datetime
