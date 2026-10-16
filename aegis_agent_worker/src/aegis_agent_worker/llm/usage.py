"""聊天模型调用用途上下文；通用用量契约位于 common.usage。"""
from contextlib import contextmanager
from contextvars import ContextVar


_purpose = ContextVar("model_call_purpose", default=None)
PURPOSES = frozenset({"main_routing", "main_routing_repair", "model_completion", "model_json",
    "knowledge_planning", "knowledge_planning_repair", "knowledge_answer", "knowledge_answer_repair"})


@contextmanager
def model_call_purpose(value):
    if value not in PURPOSES:
        raise ValueError("未知模型调用用途")
    token = _purpose.set(value)
    try:
        yield
    finally:
        _purpose.reset(token)


def current_model_purpose(json_mode):
    return _purpose.get() or ("model_json" if json_mode else "model_completion")
