"""注册的是可信代码入口与事实核验器，不是模型可扩展的专家目录。"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from aegis_agent_worker.agent.contracts.cooperation.messages import DelegationMessage, ExpertResult
from aegis_agent_worker.repository.run_repository import ClaimedRun


class TeamValidationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


ResultVerifier = Callable[[ClaimedRun, DelegationMessage, ExpertResult], Awaitable[bool]]


@dataclass(frozen=True)
class ExpertBinding:
    name: str
    description: str
    graph: Any
    verify_result: ResultVerifier
    can_return_draft: bool = False

    def __post_init__(self):
        if self.name not in {"mail", "calendar"}:
            raise ValueError("第一版仅开放邮件与日历专家")
        if not self.description.strip() or len(self.description) > 300:
            raise ValueError("专家描述无效")
        if not callable(getattr(self.graph, "ainvoke", None)) or not callable(self.verify_result):
            raise ValueError("专家必须绑定真实异步图入口与事实核验器")
        if self.can_return_draft != (self.name == "calendar"):
            raise ValueError("仅日历专家可以返回最终会议草稿")


class TeamRegistry:
    def __init__(self, bindings):
        entries = tuple(bindings)
        if any(not isinstance(item, ExpertBinding) for item in entries):
            raise ValueError("专家绑定类型无效")
        if len({item.name for item in entries}) != len(entries):
            raise ValueError("专家重复注册")
        self._bindings = MappingProxyType({item.name: item for item in entries})

    def require_ready(self):
        if set(self._bindings) != {"mail", "calendar"}:
            raise ValueError("会议协作必须绑定邮件、日历专家；不能注册占位能力")

    def get(self, name):
        if name not in self._bindings:
            raise TeamValidationError("COOPERATION_UNKNOWN_EXPERT")
        return self._bindings[name]

    def participants(self):
        return [{"name": item.name, "description": item.description,
                 "risk_level": "draft_only" if item.can_return_draft else "read"}
                for item in self._bindings.values()]

    async def verify(self, run, delegation, result):
        binding = self.get(delegation.target)
        if result.delegation_id != delegation.delegation_id or result.expert != binding.name:
            raise TeamValidationError("COOPERATION_RESULT_MISMATCH")
        if result.draft is not None and not binding.can_return_draft:
            raise TeamValidationError("COOPERATION_RESULT_RISK_BLOCKED")
        if await binding.verify_result(run, delegation, result) is not True:
            raise TeamValidationError("COOPERATION_RESULT_STALE")
