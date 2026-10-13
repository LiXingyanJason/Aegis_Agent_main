"""代码控制的任务策略；模型不能修改预算和工具白名单。"""
from dataclasses import dataclass
from .validation import positive_limits


@dataclass(frozen=True)
class TaskExecutionPolicy:
    """集中定义一次任务运行（run）的预算上限。"""

    version: str = "harness-v1"
    max_models: int = 10  # 单次 run 最多允许的模型调用次数
    max_tools: int = 6  # 单次 run 最多允许的工具调用次数
    active_seconds: int = 120  # 单次 run 累计活跃执行时长上限（秒）
    max_input_chars: int = 48000  # 输入字符上限
    max_output_chars: int = 80000  # 输出字符上限
    model_timeout_seconds: int = 60
    tool_timeout_seconds: int = 20
    tool_scopes: tuple = (
        ("conversation", frozenset({"calendar.list_events", "calendar.find_free_time", "calendar.create_event",
                                   "mail.messages.list", "conversation.cards.list", "conversation.cards.get"})),
        ("mail_extraction", frozenset({"mail.messages.get"})),
        ("mail_reply_draft", frozenset({"mail.messages.get", "mail.messages.list", "calendar.list_events", "calendar.find_free_time"})),
        ("mail_draft_revision", frozenset()), ("mail_send", frozenset({"mail.messages.send"})),
    )

    def __post_init__(self):
        positive_limits(self, ("max_models", "max_tools", "active_seconds", "max_input_chars",
                              "max_output_chars", "model_timeout_seconds", "tool_timeout_seconds"))
        if not isinstance(self.version, str) or not self.version or len(self.version) > 64:
            raise ValueError("策略版本必须为1至64字符")
        if (not isinstance(self.tool_scopes, tuple)
                or any(not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], str)
                       or not isinstance(item[1], frozenset) or not all(isinstance(name, str) for name in item[1])
                       for item in self.tool_scopes)):
            raise ValueError("工具范围必须使用不可变的tuple／frozenset声明")
        if len({item[0] for item in self.tool_scopes}) != len(self.tool_scopes):
            raise ValueError("任务类型工具范围重复")

    def tools(self, run_type: str) -> set[str]:
        # 根据 run_type（任务类型）返回该类型允许调用的工具集合。
        return set(dict(self.tool_scopes).get(run_type, frozenset()))

    def call_limit(self, kind: str) -> int:
        """只允许两种可信调用类型，避免动态SQL字段来自任意字符串。"""
        if kind == "model":
            return self.max_models
        if kind == "tool":
            return self.max_tools
        raise ValueError("未知执行调用类型")

    def call_timeout(self, kind: str) -> int:
        self.call_limit(kind)
        return self.model_timeout_seconds if kind == "model" else self.tool_timeout_seconds
