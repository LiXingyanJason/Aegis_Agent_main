"""每次Worker消费的运行上下文；随协程传播，不保存数据库资源。"""
from contextvars import ContextVar, Token
from dataclasses import dataclass
from time import monotonic

from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.repository.run_repository import ClaimedRun
from typing import Any


@dataclass(frozen=True)
class ExecutionContext:
    run: ClaimedRun
    started_at: float
    policy_bundle: Any

    @property
    def elapsed_seconds(self) -> float:
        return monotonic() - self.started_at


_runtime: ContextVar[ExecutionContext | None] = ContextVar(
    "aegis_execution_runtime", default=None,
)


def bind_execution(run: ClaimedRun, policy_bundle) -> Token:
    return _runtime.set(ExecutionContext(run=run, started_at=monotonic(), policy_bundle=policy_bundle))


def peek_execution() -> ExecutionContext | None:
    return _runtime.get()


def reset_execution(token: Token) -> None:
    _runtime.reset(token)


def get_execution() -> ExecutionContext:
    context = _runtime.get()
    if context is None:
        raise HarnessError("HARNESS_CONTEXT_REQUIRED", "缺少任务执行上下文")
    return context
