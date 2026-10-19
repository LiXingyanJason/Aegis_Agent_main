"""一次语义调用的记忆请求；只传播可信引用，不保存正文或写入Checkpoint。"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from aegis_agent_worker.llm.usage import model_call_purpose


@dataclass(frozen=True)
class MemoryRequest:
    service: object
    run: object
    purpose: str
    task_text: str


_request = ContextVar('model_memory_request', default=None)


def current_memory_request():
    return _request.get()


@contextmanager
def memory_call_scope(service, run, purpose, task_text):
    """Workflow声明用途；真正读取/注入/审计仍在GuardedLLMClient内逐次发生。"""
    token = _request.set(MemoryRequest(service, run, purpose, task_text))
    try:
        with model_call_purpose(purpose):
            yield
    finally:
        _request.reset(token)
