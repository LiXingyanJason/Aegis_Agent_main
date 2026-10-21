"""执行事件的可信关联上下文；仅传播标识，不传播正文或权限。"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field


class CooperationAssociation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    team_id: UUID
    actor: Literal["coordinator", "mail", "calendar", "approval"]
    round: int | None = Field(default=None, ge=0, strict=True)
    delegation_id: UUID | None = None
    sequence: int | None = Field(default=None, ge=1, strict=True)
    input_kind: Literal["mail_selection", "mail_requirements", "calendar_requirements"] | None = None


def association(run_id, actor="coordinator", **fields):
    return CooperationAssociation(team_id=uuid5(UUID(str(run_id)),"cooperation-team:v1"),
        actor=actor, **fields).model_dump(mode="json",exclude_none=True)


def validate_association(run_id, value):
    item=CooperationAssociation.model_validate(value)
    if str(item.team_id)!=association(run_id)["team_id"]:
        raise ValueError("协作关联不属于当前任务")
    if (item.delegation_id is None)!=(item.sequence is None):
        raise ValueError("委派关联字段不完整")
    if item.delegation_id is not None and item.actor not in {"mail","calendar"}:
        raise ValueError("该执行者不属于专家委派")
    if item.input_kind is not None and item.actor!=("calendar" if item.input_kind=="calendar_requirements" else "mail"):
        raise ValueError("补问类别与专家不一致")
    if item.delegation_id is not None and item.delegation_id!=uuid5(UUID(str(run_id)),f"team-delegation:{item.sequence}"):
        raise ValueError("委派身份不匹配")
    return item.model_dump(mode="json",exclude_none=True)


_active=ContextVar("cooperation_event_context",default=None)


@contextmanager
def cooperation_event_scope(run, value, events=None):
    value=validate_association(run.id,value)
    token=_active.set((run.id,run.tenant_id,value,events))
    try: yield
    finally: _active.reset(token)


def current_association(run_id, tenant_id):
    active=_active.get()
    return dict(active[2]) if active and (active[0],active[1])==(run_id,tenant_id) else None


async def resumed(run, question_id):
    from aegis_agent_worker.harness.context import get_execution
    active=_active.get()
    if active and active[0]==run.id and active[1]==run.tenant_id and active[3] is not None:
        await active[3].record(run,phase="resumed",round_no=active[2]["round"],
            policy_version=get_execution().policy_bundle.version,
            association=active[2],question_id=question_id)
