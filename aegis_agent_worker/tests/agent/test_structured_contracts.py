"""结构化计划、结果摘要和 Artifact 契约测试。"""

import pytest
import json
from pydantic import ValidationError

from aegis_agent_worker.agent.contracts.calendar import (
    CalendarFreeTimeCandidate,
    CalendarMeetingDraftProposal,
    CalendarReadPlan,
    CalendarSlotRecommendation,
    build_calendar_read_artifact,
)
from aegis_agent_worker.agent.contracts.common import ToolResultSummary
from aegis_agent_worker.tool.contracts import ToolInvocation


@pytest.mark.parametrize("invalid_tail", [False, True])
def test_draft_alternatives_validate_all_before_limiting(invalid_tail: bool) -> None:
    """九个合法备选保留前五项；尾部非法候选不能因截断逃过校验。"""
    candidates = [
        CalendarFreeTimeCandidate(
            slot_id=f"slot_{index:016x}",
            start_at="2026-10-12T09:00:00+08:00",
            end_at="2026-10-12T10:00:00+08:00",
        )
        for index in range(10)
    ]
    alternatives = [item.slot_id for item in candidates[1:]]
    if invalid_tail:
        alternatives[-1] = "slot_ffffffffffffffff"
    payload = json.dumps({
        "title": "总结", "description": "本周复盘", "attendees": [],
        "selected_slot_id": candidates[0].slot_id,
        "alternative_slot_ids": alternatives, "reason": "最早可用时段",
    })
    if invalid_tail:
        with pytest.raises(ValueError, match="备选时间段无效"):
            CalendarMeetingDraftProposal.from_llm_text(payload, candidates)
    else:
        proposal = CalendarMeetingDraftProposal.from_llm_text(payload, candidates)
        assert proposal.alternative_slot_ids == alternatives[:5]
        assert proposal.title == "总结"


def test_calendar_read_plan_accepts_only_matching_registered_read_tool() -> None:
    """测试日历专家的白名单调用可转换为可检查点持久化计划。"""
    plan = CalendarReadPlan.from_invocation(
        ToolInvocation(
            "calendar.list_events",
            {"start_at": "2026-10-09T00:00:00+08:00", "end_at": "2026-10-10T00:00:00+08:00"},
        )
    )

    assert plan.action == "list_events"
    assert plan.to_invocation().tool_name == "calendar.list_events"


def test_calendar_read_plan_rejects_action_that_does_not_match_tool() -> None:
    """测试 State 中被篡改的 action 与工具名组合不会进入工具调用。"""
    with pytest.raises(ValidationError):
        CalendarReadPlan(
            action="list_events",
            tool_name="calendar.find_free_time",
            arguments={"start_at": "2026-10-09T00:00:00+08:00", "end_at": "2026-10-10T00:00:00+08:00"},
        )


def test_calendar_event_artifact_keeps_only_card_safe_fields() -> None:
    """测试 Artifact 不保留描述、参会人等不属于日程卡片的字段。"""
    plan = CalendarReadPlan(
        action="list_events",
        tool_name="calendar.list_events",
        arguments={"start_at": "2026-10-09T00:00:00+08:00", "end_at": "2026-10-10T00:00:00+08:00"},
    )
    result = ToolResultSummary(
        status="succeeded",
        tool_name="calendar.list_events",
        summary={
            "display_timezone": "Asia/Shanghai",
            "events": [
                {
                    "title": "项目同步",
                    "start_at_local": "2026-10-09 15:00",
                    "end_at_local": "2026-10-09 15:30",
                    "description": "不应写进 Artifact 的完整会议描述",
                    "attendees": ["private@example.com"],
                }
            ],
        },
    )

    artifact = build_calendar_read_artifact(plan, result)

    assert artifact is not None
    assert artifact.artifact_type == "calendar_events"
    item = artifact.payload["items"][0]
    assert item["title"] == "项目同步"
    assert "description" not in item
    assert "attendees" not in item


def test_calendar_slot_recommendation_rejects_slot_not_returned_by_mcp() -> None:
    """测试模型不能推荐候选集合之外的虚构时间。"""
    candidates = [
        CalendarFreeTimeCandidate(
            slot_id="slot_0123456789abcdef",
            start_at="2026-10-09T15:00:00+08:00",
            end_at="2026-10-09T15:30:00+08:00",
        )
    ]

    with pytest.raises(ValueError):
        CalendarSlotRecommendation.from_llm_text(
            '{"selected_slot_id":"slot_fedcba9876543210","alternative_slot_ids":[],"reason":"下午优先"}',
            candidates,
        )


def test_calendar_meeting_draft_rejects_untrusted_slot_and_invalid_attendee() -> None:
    """测试 LLM 可完善草稿文字，但不能伪造时间或写入非邮箱参会人。"""
    candidates = [
        CalendarFreeTimeCandidate(
            slot_id="slot_0123456789abcdef",
            start_at="2026-10-09T15:00:00+08:00",
            end_at="2026-10-09T15:30:00+08:00",
        )
    ]

    with pytest.raises(ValueError, match="未返回的时间段"):
        CalendarMeetingDraftProposal.from_llm_text(
            (
                '{"title":"总结","description":"复盘本周工作","attendees":[],'
                '"selected_slot_id":"slot_fedcba9876543210","alternative_slot_ids":[],'
                '"reason":"下午优先"}'
            ),
            candidates,
        )

    with pytest.raises(ValidationError, match="有效邮箱"):
        CalendarMeetingDraftProposal.from_llm_text(
            (
                '{"title":"总结","description":"","attendees":["王敏"],'
                '"selected_slot_id":"slot_0123456789abcdef","alternative_slot_ids":[],'
                '"reason":"下午优先"}'
            ),
            candidates,
        )
