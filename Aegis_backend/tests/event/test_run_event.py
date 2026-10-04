"""任务 SSE 事件模型与编码的测试。"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.controller.run_controller import _format_sse_event, _parse_last_event_id
from app.event.run_event import RunEvent, run_event_channel


RUN_ID = UUID("2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6")


def _event(event_no: int = 3) -> RunEvent:
    """构造一条可序列化的运行完成事件。"""
    return RunEvent(
        run_id=RUN_ID,
        event_no=event_no,
        event_type="run_completed",
        payload={"status": "completed", "result_summary": "已完成"},
        created_at=datetime(2026, 10, 4, 8, 0, tzinfo=UTC),
    )


def test_run_event_wire_payload_round_trip() -> None:
    """测试事件可在 Redis Pub/Sub 的 JSON 文本与内部对象之间无损转换。"""
    event = _event()

    restored = RunEvent.from_wire_payload(event.to_wire_payload())

    assert restored == event
    assert run_event_channel(RUN_ID) == f"aegis:run:{RUN_ID}:events"


def test_format_sse_event_uses_resume_id_and_named_event() -> None:
    """测试 SSE 帧包含断线续传编号、事件类型和 JSON 数据。"""
    frame = _format_sse_event(_event())

    assert frame.startswith("id: 3\nevent: run_completed\ndata: ")
    assert '"status":"completed"' in frame
    assert frame.endswith("\n\n")


@pytest.mark.parametrize("value, expected", [(None, 0), ("", 0), ("18", 18)])
def test_parse_last_event_id_accepts_valid_values(value: str | None, expected: int) -> None:
    """测试未提供或合法的 Last-Event-ID 可确定历史补发起点。"""
    assert _parse_last_event_id(value) == expected


@pytest.mark.parametrize("value", ["-1", "not-a-number"])
def test_parse_last_event_id_rejects_invalid_values(value: str) -> None:
    """测试非法 Last-Event-ID 返回 422，避免错误的续传位置。"""
    with pytest.raises(HTTPException) as error:
        _parse_last_event_id(value)

    assert error.value.status_code == 422
