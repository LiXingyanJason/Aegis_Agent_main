"""各工作流的局部额度；计数仍在对应LangGraph State。"""
from dataclasses import dataclass
from .validation import positive_limits


@dataclass(frozen=True)
class MainContextPolicy:
    max_reads: int = 2
    max_rounds: int = 3
    max_context_bytes: int = 24000
    def __post_init__(self):
        positive_limits(self, ("max_reads", "max_rounds", "max_context_bytes"))


@dataclass(frozen=True)
class MailCollectionPolicy:
    max_rounds: int = 5
    max_evidence_bytes: int = 12000
    def __post_init__(self):
        positive_limits(self, ("max_rounds", "max_evidence_bytes"))
