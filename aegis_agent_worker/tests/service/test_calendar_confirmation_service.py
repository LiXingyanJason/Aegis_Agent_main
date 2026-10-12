"""会议推荐前的可信候选提取测试。"""

import pytest

from aegis_agent_worker.agent.contracts.calendar import (
    CalendarFreeTimeCandidate,
    extract_calendar_free_time_candidates,
)
from aegis_agent_worker.service.calendar.calendar_confirmation_service import _meeting_interval


def test_extract_free_time_candidates_assigns_stable_ids() -> None:
    """工具可信摘要会转换为多个带稳定标识的候选，供 LLM 排序而非直接取第一项。"""
    summary = {
        "duration_minutes": 30,
        "available_slots": [
            {"start_at": "2026-10-07T09:00:00+08:00", "end_at": "2026-10-07T09:30:00+08:00"},
            {"start_at": "2026-10-07T15:00:00+08:00", "end_at": "2026-10-07T15:30:00+08:00"},
        ],
    }

    candidates, duration = extract_calendar_free_time_candidates(summary)

    assert duration == 30
    assert len(candidates) == 2
    assert candidates[0].slot_id.startswith("slot_")
    assert candidates[0].start_at == "2026-10-07T09:00:00+08:00"


def test_extract_free_time_candidates_rejects_missing_tool_summary() -> None:
    """工具失败或格式错误时不凭空构造候选，也不会创建会议草稿。"""
    assert extract_calendar_free_time_candidates(None) == ([], None)
    assert extract_calendar_free_time_candidates({"available_slots": "invalid"}) == ([], None)


def test_meeting_interval_uses_requested_duration_not_entire_free_slot() -> None:
    """测试草稿会议只占用用户请求的 50 分钟，而不是整段空闲时间。"""
    candidate = CalendarFreeTimeCandidate(
        slot_id="slot_0123456789abcdef",
        start_at="2026-10-11T14:02:00+08:00",
        end_at="2026-10-11T18:00:00+08:00",
    )

    start_at, end_at = _meeting_interval(candidate, 50)

    assert start_at.isoformat() == "2026-10-11T14:02:00+08:00"
    assert end_at.isoformat() == "2026-10-11T14:52:00+08:00"


def test_meeting_interval_rejects_short_candidate() -> None:
    """测试模型不能用短于请求时长的候选创建会议。"""
    candidate = CalendarFreeTimeCandidate(
        slot_id="slot_0123456789abcdef",
        start_at="2026-10-11T14:02:00+08:00",
        end_at="2026-10-11T14:30:00+08:00",
    )

    with pytest.raises(ValueError, match="不足以容纳"):
        _meeting_interval(candidate, 50)
