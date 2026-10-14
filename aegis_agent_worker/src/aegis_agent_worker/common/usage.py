"""聊天和 Embedding 共用的供应商用量契约，只记录计数。"""
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
