"""领域提供的结果复核、呈现及失败适配协议。"""
from collections.abc import Awaitable,Callable
from dataclasses import dataclass
from typing import Any
from aegis_agent_worker.agent.contracts.cooperation import TaskStepResult


@dataclass(frozen=True)
class StepResultHandler:
    verify: Callable[[Any,dict,TaskStepResult],Awaitable[None]]
    failure_issue: str
    present: Callable[[dict],str] | None = None
    failure_updates: tuple[tuple[str,str],...] = ()
    failure_content: str = '协同步骤未完成，已停止后续执行；不会自动重试。'
