"""Redis 队列 Trace Context 传播测试。"""

import json
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from app.observability.structured_logging import get_current_trace_id
from app.observability.trace_context import deserialize_run_message, serialize_run_message


def test_queue_message_propagates_w3c_traceparent() -> None:
    """测试 API 投递的任务会携带可被 Worker 恢复的 W3C traceparent。"""
    run_id = UUID("d99ce16b-7970-46b7-8d18-6b953fdfb5fa")
    source_context = SpanContext(
        trace_id=0x1234567890ABCDEF1234567890ABCDEF,
        span_id=0x1234567890ABCDEF,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    with trace.use_span(NonRecordingSpan(source_context), end_on_exit=False):
        assert get_current_trace_id() == "1234567890abcdef1234567890abcdef"
        raw_message = serialize_run_message(run_id)

    wire_payload = json.loads(raw_message)
    assert wire_payload["run_id"] == str(run_id)
    assert wire_payload["traceparent"].startswith("00-1234567890abcdef1234567890abcdef-")

    restored = deserialize_run_message(raw_message)
    restored_span_context = trace.get_current_span(restored.context).get_span_context()
    assert restored.run_id == run_id
    assert restored_span_context.trace_id == source_context.trace_id
    assert restored_span_context.span_id == source_context.span_id
    assert restored_span_context.is_remote is True


def test_queue_message_accepts_legacy_plain_run_id() -> None:
    """测试升级前 Redis 中只含 UUID 的任务不会因新格式上线而无法消费。"""
    run_id = UUID("d99ce16b-7970-46b7-8d18-6b953fdfb5fa")

    restored = deserialize_run_message(str(run_id))

    assert restored.run_id == run_id
    assert trace.get_current_span(restored.context).get_span_context().is_valid is False


def test_queue_message_carries_approval_resume_decision() -> None:
    """测试批准接口可将决定与 run_id 一起投递给独立 Worker。"""
    run_id = UUID("d99ce16b-7970-46b7-8d18-6b953fdfb5fa")

    restored = deserialize_run_message(serialize_run_message(run_id, resume_decision="approved"))

    assert restored.run_id == run_id
    assert restored.resume_decision == "approved"
