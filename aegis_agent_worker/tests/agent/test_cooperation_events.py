"""8.6事件关联、幂等落库、提交后发布与补问就绪；依赖均为本地替身。"""
import json
from contextlib import asynccontextmanager
from datetime import datetime,UTC
from uuid import UUID,uuid5

import pytest

from aegis_agent_worker.config.database import tenant_transaction,TenantContext
from aegis_agent_worker.event.cooperation_context import association,cooperation_event_scope,validate_association
from aegis_agent_worker.event.run_event import RunEvent,RunEventPublisher
from aegis_agent_worker.repository.run_repository import AgentRunRepository
from aegis_agent_worker.service.cooperation.team_event_service import TeamEventService
from aegis_agent_worker.service.runtime.input_request_service import InputRequestService
from test_cooperation_runtime import RUN
from test_harness_react_revision import Result


def correlation():
    return association(RUN.id,"mail",round=1,sequence=1,delegation_id=uuid5(RUN.id,"team-delegation:1"))


class EventsDatabase:
    def __init__(self): self.rows=[];self.ready=False;self.committed=False
    @asynccontextmanager
    async def session(self): yield self
    @asynccontextmanager
    async def begin(self):
        before=list(self.rows);ready=self.ready
        self.committed=False
        try: yield
        except BaseException:
            self.rows=before;self.ready=ready
            raise
        else: self.committed=True
    async def execute(self,sql,args):
        sql=str(sql)
        if "SELECT event_no FROM run_events" in sql:
            return Result(next((dict(event_no=index+1) for index,item in enumerate(self.rows)
                if item.payload.get("cooperation_event_id")==args["key"]),None))
        if "UPDATE run_input_requests SET ready=true" in sql:
            if self.ready: return Result()
            self.ready=True
            return Result(dict(question="请选择邮件",version=1,expires_at=datetime(2026,12,1,tzinfo=UTC)))
        if "SELECT policy_version" in sql: return Result(dict(policy_version="harness-v5-team-calendar"))
        return Result()


class Runs:
    async def append_event(self,session,**kwargs):
        event=RunEvent(kwargs["run_id"],len(session.rows)+1,kwargs["event_type"],kwargs["payload"],datetime.now(UTC))
        session.rows.append(event)
        return event


def publisher(db):
    sent=[]
    class Redis:
        async def publish(self,channel,payload):
            assert db.committed
            sent.append(json.loads(payload))
    return RunEventPublisher(Redis()),sent


@pytest.mark.asyncio
async def test_phase_replay_has_stable_identity_and_publishes_only_committed_event():
    db=EventsDatabase();pub,sent=publisher(db)
    service=TeamEventService(db,Runs(),pub)
    for _ in range(2):
        await service.record(RUN,phase="expert_started",round_no=1,policy_version="harness-v5-team-calendar",
            association=correlation())
    assert len(db.rows)==len(sent)==1
    payload=db.rows[0].payload
    assert payload["cooperation"]["actor"]=="mail" and payload["label"]=="专家开始处理"
    assert not {"goal","question","summary","body_text","prompt"}&payload.keys()


@pytest.mark.asyncio
async def test_rollback_does_not_publish_or_leave_event_for_replay():
    db=EventsDatabase();pub,sent=publisher(db)
    with pytest.raises(RuntimeError):
        async with tenant_transaction(db,TenantContext(RUN.tenant_id,RUN.user_id)):
            await TeamEventService(db,Runs(),pub).record(RUN,phase="started",round_no=0,
                policy_version="harness-v5-team-calendar",session=db)
            raise RuntimeError("rollback")
    assert not db.rows and not sent


@pytest.mark.asyncio
async def test_input_ready_publishes_correlation_once_after_checkpoint_and_keeps_question_separate():
    db=EventsDatabase();pub,sent=publisher(db)
    value={**correlation(),"input_kind":"mail_selection"}
    service=InputRequestService(db,Runs(),pub)
    for _ in range(2): await service.mark_ready(RUN,UUID(int=777),cooperation=value)
    assert [event.event_type for event in db.rows]==["input_required","cooperation_progress"]
    assert len(sent)==2
    assert db.rows[0].payload["cooperation"]["input_kind"]=="mail_selection"
    assert db.rows[1].payload["phase"]=="waiting_input" and "question" not in db.rows[1].payload


@pytest.mark.parametrize("changes",[
    dict(team_id=str(UUID(int=2))),dict(delegation_id=str(UUID(int=3))),dict(actor="hacker"),
    dict(sequence=None),dict(body_text="secret"),dict(input_kind="approve_write"),
])
def test_association_rejects_cross_task_or_arbitrary_fields(changes):
    with pytest.raises(ValueError): validate_association(RUN.id,{**correlation(),**changes})


@pytest.mark.asyncio
async def test_real_event_repository_tags_tools_and_does_not_leak_scope_to_other_tasks():
    class Session:
        async def execute(self,sql,args):
            if "INSERT INTO run_events" in str(sql):
                return Result(dict(event_no=1,event_type=args["event_type"],payload=json.loads(args["payload"]),created_at=datetime.now(UTC)))
            return Result()
    async def append(run_id=RUN.id):
        return await AgentRunRepository().append_event(Session(),run_id=run_id,tenant_id=RUN.tenant_id,
            event_type="tool_preview",payload=dict(tool_name="mail.messages.list",status="succeeded"))
    with cooperation_event_scope(RUN,correlation()):
        own=await append();other=await append(UUID(int=999))
    outside=await append()
    assert own.payload["cooperation"]==correlation()
    assert "cooperation" not in other.payload and "cooperation" not in outside.payload
