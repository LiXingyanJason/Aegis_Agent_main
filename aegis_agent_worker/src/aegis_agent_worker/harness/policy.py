"""代码控制的任务策略；模型不能修改预算和工具白名单。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskExecutionPolicy:
    """集中定义一次任务运行（run）的预算上限。"""

    version: str = "harness-v1"
    max_models: int = 10  # 单次 run 最多允许的模型调用次数
    max_tools: int = 6  # 单次 run 最多允许的工具调用次数
    active_seconds: int = 120  # 单次 run 累计活跃执行时长上限（秒）
    max_input_chars: int = 48000  # 输入字符上限
    max_output_chars: int = 80000  # 输出字符上限

    def tools(self, run_type: str) -> set[str]:
        # 根据 run_type（任务类型）返回该类型允许调用的工具集合。
        return {
            "conversation": {
                "calendar.list_events", "calendar.find_free_time",
                "calendar.create_event", "mail.messages.list",
                "conversation.cards.list", "conversation.cards.get",
            },
            "mail_extraction": {"mail.messages.get"},
            "mail_reply_draft": {
                "mail.messages.get", "mail.messages.list",
                "calendar.list_events", "calendar.find_free_time",
            },
            "mail_draft_revision": set(),
            "mail_send": {"mail.messages.send"},
        }.get(run_type, set())

    def call_limit(self, kind: str) -> int:
        """只允许两种可信调用类型，避免动态SQL字段来自任意字符串。"""
        if kind == "model":
            return self.max_models
        if kind == "tool":
            return self.max_tools
        raise ValueError("未知执行调用类型")

    def call_timeout(self, kind: str) -> int:
        self.call_limit(kind)
        return 60 if kind == "model" else 20
