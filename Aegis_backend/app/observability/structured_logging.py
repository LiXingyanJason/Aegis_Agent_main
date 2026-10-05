"""输出不含敏感正文的 JSON 结构化日志。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace


# 只允许业务代码显式写入这些字段，避免将 access token、邮件正文或模型提示词意外记录到日志。
_ALLOWED_FIELDS = frozenset(
    {
        "event",
        "run_id",
        "conversation_id",
        "tool_call_id",
        "queue_name",
        "status",
        "error_code",
        "duration_ms",
        "provider",
        "model_name",
        "tool_name",
    }
)
_configured = False


class JsonFormatter(logging.Formatter):
    """将日志记录转换为便于 Loki、ELK 等系统采集的一行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        """附加当前 Span 的 trace_id/span_id，并安全序列化允许的业务字段。"""
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _ALLOWED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = str(value) if field.endswith("_id") else value

        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_structured_logging() -> None:
    """为当前进程的根日志器安装一次 JSON Formatter，避免热重载重复添加 Handler。"""
    global _configured
    if _configured:
        return

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    # Uvicorn 默认给自身 logger 安装独立 Handler，需单独替换其 Formatter 才能使访问日志也保持 JSON。
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for uvicorn_handler in logging.getLogger(logger_name).handlers:
            uvicorn_handler.setFormatter(JsonFormatter())
    _configured = True


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """记录具有受限字段集的业务事件，避免调用方把敏感内容直接传给日志。"""
    extra = {"event": event}
    extra.update({key: value for key, value in fields.items() if key in _ALLOWED_FIELDS})
    logger.log(level, event, extra=extra)


def get_current_trace_id() -> str | None:
    """返回当前有效 Trace 的 32 位十六进制标识；无追踪上下文时返回空值。"""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return format(span_context.trace_id, "032x")
