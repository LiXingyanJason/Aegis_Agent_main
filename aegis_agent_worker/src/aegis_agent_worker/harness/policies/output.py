"""结构化输出修复、单次卡片和工具结果边界。"""
from dataclasses import dataclass
from .validation import positive_limits


@dataclass(frozen=True)
class OutputPolicy:
    max_repairs: int = 1
    read_tool_retries: int = 1
    card_result_bytes: int = 12000
    tool_result_bytes: int = 2_000_000
    mail_source_chars: int = 12000
    repair_response_chars: int = 12000
    history_items: int = 8
    history_chars: int = 12000
    user_message_chars: int = 4000
    def __post_init__(self):
        positive_limits(self, ("card_result_bytes", "tool_result_bytes", "mail_source_chars", "repair_response_chars",
                              "history_items", "history_chars", "user_message_chars"))
        for name in ("max_repairs", "read_tool_retries"):
            if type(getattr(self, name)) is not int or not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"策略字段{name}仅允许0或1")
