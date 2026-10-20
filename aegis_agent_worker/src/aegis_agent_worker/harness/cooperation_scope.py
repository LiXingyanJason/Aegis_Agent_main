"""用现有attempt风险标签关联协同步骤；不改变预算或授予工具权限。"""
import re
from contextlib import contextmanager
from contextvars import ContextVar

_step=ContextVar('cooperation_step_tags',default=())


def step_tags(step_id,plan_digest):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,31}',step_id) or not re.fullmatch(r'[a-f0-9]{64}',plan_digest):
        raise ValueError('无效协同步骤审计绑定')
    return (f'cooperation_step:{step_id}',f'cooperation_plan:{plan_digest}')


def current_step_tags():
    return _step.get()


@contextmanager
def cooperation_step_scope(step_id,plan_digest):
    token=_step.set(step_tags(step_id,plan_digest))
    try: yield
    finally: _step.reset(token)
