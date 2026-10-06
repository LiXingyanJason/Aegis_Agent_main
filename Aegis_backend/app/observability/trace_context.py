"""在 API Redis 投递与 Worker 消费之间传播 W3C Trace Context。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from opentelemetry.context import Context
from opentelemetry.propagate import extract
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


@dataclass(frozen=True, slots=True)
class QueuedRunMessage:
    """Redis 队列中的最小任务消息；兼容历史仅含 run_id 的字符串。"""

    run_id: UUID
    context: Context
    resume_decision: str | None = None


def serialize_run_message(run_id: UUID, *, resume_decision: str | None = None) -> str:
    """把当前请求的 W3C traceparent 与任务标识一起写入 Redis。"""
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    if resume_decision not in {None, "approved", "rejected"}:
        raise ValueError("resume_decision 必须是 approved 或 rejected")
    payload = {"run_id": str(run_id)}
    if resume_decision is not None:
        payload["resume_decision"] = resume_decision
    for key in ("traceparent", "tracestate"):
        if key in carrier:
            payload[key] = carrier[key]
    return json.dumps(payload, separators=(",", ":"))


def deserialize_run_message(raw_message: str) -> QueuedRunMessage:
    """解析队列消息并恢复上游 Trace；旧格式 UUID 字符串仍然可被安全消费。"""
    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError:
        return QueuedRunMessage(run_id=UUID(raw_message), context=Context())

    if not isinstance(payload, dict) or not isinstance(payload.get("run_id"), str):
        raise ValueError("Agent 队列消息缺少有效 run_id")
    carrier = {
        key: value
        for key in ("traceparent", "tracestate")
        if isinstance((value := payload.get(key)), str)
    }
    resume_decision = payload.get("resume_decision")
    if resume_decision not in {None, "approved", "rejected"}:
        raise ValueError("Agent 队列消息包含无效 resume_decision")
    return QueuedRunMessage(
        run_id=UUID(payload["run_id"]), context=extract(carrier), resume_decision=resume_decision
    )
