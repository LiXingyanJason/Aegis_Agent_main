"""补充回答与正文修改接口的受保护契约测试。"""
from uuid import UUID
from datetime import UTC,datetime,timedelta
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.controller import agent_input_controller as module
from app.param.agent_input_param import InputAnswerParam,DraftRevisionParam
from app.repository.agent_input_repository import AgentInputRepository
from app.security.dependencies import CurrentUser,get_current_user,get_tenant_session
from app.repository.user_repository import LocalUser

USER=LocalUser(id=UUID(int=1),tenant_id=UUID(int=2),email="jason@example.com",display_name="Jason")
QID,RUN,DRAFT=UUID(int=3),UUID(int=4),UUID(int=5)


class Dispatcher:
    def __init__(self): self.calls=[]
    def enqueue_after_commit(self,run,**kwargs): self.calls.append((run,kwargs))


class Service:
    async def answer(self,session,user,id,param):
        assert user==USER and id==QID
        return {"run_id":RUN,"question_id":QID,"execution_scheduled":True,"idempotent_replay":False}
    async def create_revision(self,session,user,id,param):
        assert user==USER and id==DRAFT and param.expected_version==1
        return {"run_id":RUN,"status":"queued","idempotent_replay":False}


def app(monkeypatch,authorized=True):
    application=FastAPI(); application.include_router(module.router)
    dispatcher=Dispatcher()
    monkeypatch.setattr(module,"service",Service())
    if authorized:
        application.dependency_overrides[get_current_user]=lambda:CurrentUser(USER)
    async def session(): yield None
    application.dependency_overrides[get_tenant_session]=session
    application.dependency_overrides[module.get_run_dispatcher_from_request]=lambda:dispatcher
    return application,dispatcher


def test_input_restore_uses_question_id_not_approval(monkeypatch):
    """回答接口投递独立问题恢复消息，不能混入批准决定。"""
    application,dispatch=app(monkeypatch)
    with TestClient(application) as client:
        response=client.post(f"/input-requests/{QID}/answers",json={"expected_version":1,"client_response_id":"client-1","response":"价格100元"})
    assert response.status_code==200
    assert dispatch.calls==[(RUN,{"resume_input_id":QID})]


def test_revision_is_async_and_cannot_change_recipients(monkeypatch):
    """正文修改仅接收版本和补充要求，额外收件人字段拒绝。"""
    application,dispatch=app(monkeypatch)
    data={"expected_version":1,"client_request_id":"client-1","instruction":"语气正式"}
    with TestClient(application) as client:
        assert client.post(f"/mail/drafts/{DRAFT}/revision-requests",json={**data,"to":["other@example.com"]}).status_code==422
        result=client.post(f"/mail/drafts/{DRAFT}/revision-requests",json=data)
    assert result.status_code==202
    assert result.json()["data"]["events_url"]==f"/runs/{RUN}/events"
    assert dispatch.calls==[(RUN,{})]


def test_anonymous_answer_rejected(monkeypatch):
    application,_=app(monkeypatch,False)
    with TestClient(application) as client:
        result=client.post(f"/input-requests/{QID}/answers",json={"expected_version":1,"client_response_id":"a","response":"answer"})
    assert result.status_code==401


class Result:
    def __init__(self,row): self.row=row
    def mappings(self): return self
    def first(self): return self.row


class Session:
    def __init__(self,row): self.row=row; self.calls=[]
    async def execute(self,sql,args):
        self.calls.append((str(sql),args))
        return Result(self.row if len(self.calls)==1 else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes",[
    {"ready":False},{"expired":True},{"version":2},{"run_status":"running"},{"status":"cancelled"},
])
async def test_answer_race_expiry_version_and_owner_conditions(changes):
    """检查点未就绪、过期、旧版本和非等待任务不能接受回答，SQL总是限定归属。"""
    row=dict(run_id=RUN,ready=True,expired=False,version=1,status="pending",run_status="waiting_input")
    session=Session({**row,**changes})
    param=InputAnswerParam(expected_version=1,client_response_id="c",response="answer")
    assert await AgentInputRepository().answer(session,USER,QID,param)=="conflict"
    assert len(session.calls)==1
    assert session.calls[0][1]["tenant"]==USER.tenant_id and session.calls[0][1]["user"]==USER.id


@pytest.mark.asyncio
async def test_replayed_answer_only_requeues_unconsumed_waiting_task():
    """已提交未消费回答允许安全修复丢失投递，已消费回答不能再次恢复。"""
    row=dict(run_id=RUN,ready=True,expired=False,version=1,status="answered",run_status="waiting_input",
             client_response_id="c",response="answer")
    param=InputAnswerParam(expected_version=1,client_response_id="c",response="answer")
    result=await AgentInputRepository().answer(Session(row),USER,QID,param)
    assert result["execution_scheduled"] and result["idempotent_replay"]
    result=await AgentInputRepository().answer(Session({**row,"status":"consumed","run_status":"running"}),USER,QID,param)
    assert not result["execution_scheduled"]
