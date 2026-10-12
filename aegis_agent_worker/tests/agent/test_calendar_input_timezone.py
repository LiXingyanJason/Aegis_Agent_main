"""用户指定会议时间的时区和查询边界回归测试。"""

import pytest

from aegis_agent_worker.agent.specialists.calendar_agent import CalendarAgent


@pytest.mark.parametrize("content", [
    "请在2026-10-12 11:00～11:50安排一个50分钟的会议",
    "请在2026-10-12T11:00:00～2026-10-12T11:50:00安排50分钟会议",
    "请在2026年10月12日11:00到11:50安排50分钟会议",
    "请在2026-10-12T03:00:00+00:00～2026-10-12T03:50:00+00:00安排50分钟会议",
    "请在2026-10-12T03:00:00Z安排50分钟会议",
])
def test_meeting_query_uses_beijing_time_and_honors_explicit_offsets(content: str) -> None:
    """无偏移时间默认北京时间，明确 UTC 输入换算后约束同一查询区间。"""
    arguments = CalendarAgent().plan_meeting(content).availability_invocation.arguments
    assert arguments["start_at"] == "2026-10-12T11:00:00+08:00"
    assert arguments["end_at"] == "2026-10-12T11:50:00+08:00"
    assert arguments["timezone"] == "Asia/Shanghai"


def test_meeting_rejects_reversed_explicit_range() -> None:
    """反向时间范围不能静默退回未来七天查询。"""
    with pytest.raises(ValueError):
        CalendarAgent().plan_meeting("2026-10-12 11:50～11:00安排会议")
