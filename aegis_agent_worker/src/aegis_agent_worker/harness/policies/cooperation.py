"""协作局部控制，不能授予工具、审批或独立的调用账户。"""
from dataclasses import dataclass

from .validation import positive_limits


@dataclass(frozen=True)
class CooperationPolicy:
    enabled: bool = False
    max_rounds: int = 6
    max_delegations: int = 4
    max_context_bytes: int = 16000
    max_repeated_results: int = 2

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("协作开关必须为bool")
        positive_limits(self, ("max_rounds", "max_delegations", "max_context_bytes",
                               "max_repeated_results"))
        if self.max_delegations >= self.max_rounds:
            raise ValueError("协调轮次必须为结束决策预留至少一轮")
        if self.max_delegations > 32:
            raise ValueError("委派上限不能超过团队结果契约容量")


@dataclass(frozen=True)
class MailMeetingPolicy:
    enabled: bool = False
    max_rounds: int = 8
    max_candidates: int = 10
    max_context_bytes: int = 18000
    max_answers: int = 4
    max_empty_searches: int = 2

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("邮件会议专家开关必须为bool")
        positive_limits(self, ("max_rounds", "max_candidates", "max_context_bytes", "max_answers", "max_empty_searches"))
        if self.max_candidates > 20:
            raise ValueError("候选上限不能超过邮件查询契约")


@dataclass(frozen=True)
class CalendarTeamPolicy:
    enabled: bool = False
    max_rounds: int = 8
    max_candidates: int = 10
    max_context_bytes: int = 18000
    max_answers: int = 4

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("日历协作专家开关必须为bool")
        positive_limits(self, ("max_rounds", "max_candidates", "max_context_bytes", "max_answers"))
        if self.max_candidates > 10:
            raise ValueError("日历候选上限不能超过现有候选提取契约")
