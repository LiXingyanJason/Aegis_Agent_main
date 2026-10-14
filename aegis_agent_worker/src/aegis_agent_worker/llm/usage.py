"""供应商用量及调用用途；仅保存计数，不保存提示词或模型输出。"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def from_payload(cls, payload):
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return cls()
        def count(name):
            value = usage.get(name)
            # bool、浮点、字符串及负数均不是有效的供应商 Token 计数。
            return value if type(value) is int and 0 <= value <= 9223372036854775807 else None
        return cls(*(count(name) for name in ("prompt_tokens", "completion_tokens", "total_tokens")))


_purpose = ContextVar("model_call_purpose", default=None)
PURPOSES = frozenset({"main_routing", "main_routing_repair", "model_completion", "model_json"})


@contextmanager
def model_call_purpose(value):
    if value not in PURPOSES:
        raise ValueError("未知模型调用用途")
    token = _purpose.set(value)
    try:
        yield
    finally:
        _purpose.reset(token)


def current_model_purpose(json_mode):
    return _purpose.get() or ("model_json" if json_mode else "model_completion")
