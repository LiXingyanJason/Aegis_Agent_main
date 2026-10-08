"""会议草稿准备阶段的纯函数测试。"""

from aegis_agent_worker.service.calendar.calendar_confirmation_service import _first_free_slot


def test_first_free_slot_only_reads_trusted_tool_summary() -> None:
    """工具可信摘要含空闲时段时，选择第一个候选时段。"""
    context = '日历工具结果\n{"tool_result":{"available_slots":[{"start_at":"2026-10-07T09:00:00+08:00","end_at":"2026-10-07T09:30:00+08:00"}]}}'

    slot = _first_free_slot(context)

    assert slot is not None
    assert slot[0].isoformat() == "2026-10-07T09:00:00+08:00"
    assert slot[1].isoformat() == "2026-10-07T09:30:00+08:00"


def test_first_free_slot_does_not_treat_failure_text_as_calendar_data() -> None:
    """工具失败文本没有结构化结果时，不得凭空生成会议草稿。"""
    assert _first_free_slot("日历工具调用失败：服务不可用") is None
