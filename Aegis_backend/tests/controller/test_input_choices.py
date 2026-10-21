"""单选回答契约、快照/权限校验、幂等及迁移DDL；不访问真实数据库。"""
from types import SimpleNamespace
from uuid import UUID
import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.param.agent_input_param import InputAnswerParam
from app.repository.agent_input_repository import AgentInputRepository

RUN,QID,EMAIL=UUID(int=10),UUID(int=20),UUID(int=30)
USER=SimpleNamespace(id=UUID(int=2),tenant_id=UUID(int=1))
OPTION=dict(id="option-a",label="续约会议A",reference=dict(resource_type="email",id=str(EMAIL),source_version="v1"))


class Result:
    def __init__(self,row): self.row=row
    def mappings(self): return self
    def first(self): return self.row


class Session:
    def __init__(self,**changes):
        self.row=dict(run_id=RUN,input_type="single_choice",options=[OPTION],selected_option_id=None,
            response=None,client_response_id=None,status="pending",ready=True,expired=False,version=1,
            run_status="waiting_input")|changes
        self.calls=[]
    async def execute(self,sql,args):
        sql=str(sql);self.calls.append((sql,args))
        if "SELECT q.*" in sql: return Result(self.row)
        if "SELECT em.id" in sql: return Result(None if getattr(self,"revoked",False) else dict(id=EMAIL))
        return Result(None)


@pytest.mark.parametrize("payload",[{},dict(response="a",selected_option_id="option-a"),
    dict(selected_option_id=" "),dict(response=" "),dict(selected_option_id="x",approved=True)])
def test_answer_requires_exactly_one_safe_answer_field(payload):
    with pytest.raises(ValidationError): InputAnswerParam(expected_version=1,client_response_id="c",**payload)


@pytest.mark.asyncio
async def test_choice_persists_id_not_label_and_checks_owned_current_mail():
    session=Session()
    result=await AgentInputRepository().answer(session,USER,QID,
        InputAnswerParam(expected_version=1,client_response_id="c",selected_option_id="option-a"))
    assert result["execution_scheduled"]
    sql,args=session.calls[-1]
    assert args["selected"]=="option-a" and args["response"] is None
    assert "em.source_version=:version" in session.calls[1][0]
    assert session.calls[1][1]["tenant"]==USER.tenant_id and session.calls[1][1]["user"]==USER.id


@pytest.mark.asyncio
@pytest.mark.parametrize("payload",[dict(selected_option_id="invented"),dict(response="第一封")])
async def test_single_choice_does_not_accept_text_or_invented_option(payload):
    session=Session()
    result=await AgentInputRepository().answer(session,USER,QID,InputAnswerParam(expected_version=1,client_response_id="c",**payload))
    assert result=="conflict" and len(session.calls)==1


@pytest.mark.asyncio
async def test_revoked_or_changed_source_rejects_choice_without_answer_write():
    session=Session();session.revoked=True
    result=await AgentInputRepository().answer(session,USER,QID,
        InputAnswerParam(expected_version=1,client_response_id="c",selected_option_id="option-a"))
    assert result=="conflict" and len(session.calls)==2


@pytest.mark.asyncio
@pytest.mark.parametrize("status,schedule",[("answered",True),("consumed",False)])
async def test_same_choice_replay_is_idempotent_and_consumed_never_resumes_twice(status,schedule):
    session=Session(status=status,client_response_id="c",selected_option_id="option-a")
    param=InputAnswerParam(expected_version=1,client_response_id="c",selected_option_id="option-a")
    result=await AgentInputRepository().answer(session,USER,QID,param)
    assert result["idempotent_replay"] and result["execution_scheduled"]==schedule
    session.row['options']=[OPTION,{**OPTION,"id":"option-b"}]
    different=InputAnswerParam(expected_version=1,client_response_id="c",selected_option_id="option-b")
    assert await AgentInputRepository().answer(session,USER,QID,different)=="conflict"


@pytest.mark.asyncio
async def test_old_text_question_stays_text_and_cannot_accept_option_id():
    session=Session(input_type="text",options=[])
    param=InputAnswerParam(expected_version=1,client_response_id="c",selected_option_id="option-a")
    assert await AgentInputRepository().answer(session,USER,QID,param)=="conflict"


def test_migration_is_additive_and_downgrade_requires_drain(monkeypatch):
    path=Path(__file__).resolve().parents[2]/"migrations/versions/021_structured_input_choices.py"
    spec=importlib.util.spec_from_file_location("input_choices_migration",path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    statements=[];monkeypatch.setattr(module.op,"execute",statements.append)
    module.upgrade()
    assert module.down_revision=="020_memory_selection_revoke"
    assert "DEFAULT 'text'" in statements[0] and "response IS NULL" in statements[-1]
    statements.clear();module.downgrade()
    assert "RAISE EXCEPTION" in statements[0] and "Drain active" in statements[0]
