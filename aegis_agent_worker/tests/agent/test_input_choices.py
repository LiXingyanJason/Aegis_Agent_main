"""邮件选择ID不经过语言解析；问题快照、版本及旧文本Checkpoint兼容。"""
import json
from datetime import datetime,UTC
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
from langgraph.types import Command
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph,START,END

from aegis_agent_worker.agent.contracts.input_choice import choice_id,validate_options
from aegis_agent_worker.agent.workflows.mail.experts.meeting_state import MailMeetingState
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.service.runtime.input_request_service import InputRequestService
from test_mail_meeting_requirements import setup,SEARCH,read,EXTRACT,RETURN,initial,reference,RUN
from test_harness_react_revision import Result


@pytest.mark.parametrize("kind,options",[
    ("single_choice",[]),("text",[dict(id="x",label="邮件",reference=reference())]),
    ("single_choice",[dict(id="x",label="邮件",reference={**reference(),"source_version":None})]),
    ("single_choice",[dict(id="x",label="邮件",reference=reference())]*2),
])
def test_invalid_choice_snapshots_rejected_before_database(kind,options):
    with pytest.raises(ValueError): validate_options(kind,options)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer",[dict(response="第一封"),dict(selected_option_id="invented")])
async def test_new_choice_question_rejects_free_text_or_forged_id_without_body_read(answer):
    env=setup([SEARCH,read(2)],count=2)
    graph=StateGraph(MailMeetingState)
    graph.add_node("mail",env.workflow.build());graph.add_edge(START,"mail");graph.add_edge("mail",END)
    graph=graph.compile(checkpointer=MemorySaver());config={"configurable":{"thread_id":str(RUN.id)}}
    async with env.harness.scope(RUN): first=await graph.ainvoke(initial(),config)
    question=first['__interrupt__'][0].value
    assert question['input_type']=='single_choice' and len(question['options'])==2
    assert all(set(item)=={'id','label'} for item in question['options'])
    async with env.harness.scope(RUN):
        with pytest.raises(HarnessError): await graph.ainvoke(Command(resume={'question_id':question['question_id'],**answer}),config)
    assert len(env.store.tool_calls)==1


class DB:
    def __init__(self): self.question=None;self.events=[]
    @asynccontextmanager
    async def session(self): yield self
    @asynccontextmanager
    async def begin(self): yield
    async def execute(self,sql,args):
        sql=str(sql)
        if 'SELECT id,version,question,status,input_type' in sql: return Result(self.question)
        if 'INSERT INTO run_input_requests' in sql:
            self.question=dict(id=args['id'],version=1,question=args['question'],status='pending',valid=True,
                input_type=args['input_type'],options=json.loads(args['options']))
            return Result(self.question)
        if 'UPDATE run_input_requests SET ready=true' in sql:
            self.question['ready']=True
            return Result(self.question|dict(expires_at=datetime(2026,12,1,tzinfo=UTC)))
        if 'UPDATE agent_runs ar' in sql:
            if self.question.get('status')!='answered': return Result()
            return Result(dict(id=RUN.id,tenant_id=RUN.tenant_id,user_id=RUN.user_id,
                conversation_id=RUN.conversation_id,run_type=RUN.run_type))
        if "UPDATE run_input_requests SET status='consumed'" in sql:
            self.question['status']='consumed'
            return Result(self.question)
        return Result()


@pytest.mark.asyncio
async def test_prepare_persists_private_snapshot_reuses_only_identical_and_returns_public_options():
    db=DB();service=InputRequestService(db,None)
    qid=UUID(int=777)
    options=[dict(id=choice_id(qid,reference()),label='续约邮件',reference=reference())]
    assert options[0]['id']!=choice_id(UUID(int=778),reference())
    assert options[0]['id']!=choice_id(qid,{**reference(),'source_version':'v2'})
    first=await service.prepare(RUN,'选择邮件',request_id=qid,input_type='single_choice',options=options)
    second=await service.prepare(RUN,'选择邮件',request_id=qid,input_type='single_choice',options=options)
    assert first==second and set(first['options'][0])=={'id','label'}
    assert db.question['options'][0]['reference']['source_version']=='v1'
    with pytest.raises(HarnessError):
        await service.prepare(RUN,'选择邮件',request_id=qid,input_type='single_choice',options=[{**options[0],'label':'改动'}])


@pytest.mark.asyncio
async def test_old_text_question_checkpoint_still_resolves_exact_ordinal(monkeypatch):
    env=setup([])
    state=initial()|dict(input_request={'question_id':str(UUID(int=777))},input_request_id=str(UUID(int=777)),
        mail_meeting_candidates=[dict(subject='旧邮件',reference=reference())],mail_meeting_answers=[],mail_meeting_selected={})
    import aegis_agent_worker.agent.workflows.mail.experts.meeting_requirements_workflow as module
    monkeypatch.setattr(module,'interrupt',lambda _:dict(question_id=state['input_request_id'],response='第一封'))
    async with env.harness.scope(RUN): result=env.workflow._wait(state)
    assert result['mail_meeting_selected']==reference()


@pytest.mark.asyncio
async def test_choice_ready_event_is_public_and_claim_returns_only_selected_id_once():
    db=DB()
    class Runs:
        async def append_event(self,session,**kwargs): db.events.append(kwargs['payload'])
    service=InputRequestService(db,Runs());qid=UUID(int=777)
    option=dict(id=choice_id(qid,reference()),label='续约邮件',reference=reference())
    await service.prepare(RUN,'选择邮件',request_id=qid,input_type='single_choice',options=[option])
    await service.mark_ready(RUN,qid)
    assert db.events[0]['input_type']=='single_choice' and set(db.events[0]['options'][0])=={'id','label'}
    db.question.update(status='answered',selected_option_id=option['id'],response=None)
    run,value=await service.claim(RUN.id,qid)
    assert run.id==RUN.id and value==dict(question_id=str(qid),selected_option_id=option['id'])
    assert await service.claim(RUN.id,qid)==(None,None)


@pytest.mark.asyncio
async def test_claim_rejects_altered_private_mapping_instead_of_trusting_id_alone():
    db=DB();service=InputRequestService(db,None);qid=UUID(int=777)
    option=dict(id=choice_id(qid,reference()),label='续约邮件',reference=reference())
    await service.prepare(RUN,'选择邮件',request_id=qid,input_type='single_choice',options=[option])
    db.question.update(status='answered',selected_option_id=option['id'])
    db.question['options'][0]['reference']['source_version']='v2'
    with pytest.raises(HarnessError): await service.claim(RUN.id,qid)


@pytest.mark.asyncio
@pytest.mark.parametrize("next_action",[read(2),SEARCH])
async def test_model_cannot_replace_explicitly_selected_mail_by_reading_or_searching_another(next_action):
    env=setup([SEARCH,read(2),next_action],count=2)
    graph=StateGraph(MailMeetingState)
    graph.add_node('mail',env.workflow.build());graph.add_edge(START,'mail');graph.add_edge('mail',END)
    graph=graph.compile(checkpointer=MemorySaver());config={'configurable':{'thread_id':str(RUN.id)}}
    async with env.harness.scope(RUN): first=await graph.ainvoke(initial(),config)
    qid=first['__interrupt__'][0].value['question_id']
    async with env.harness.scope(RUN):
        result=await graph.ainvoke(Command(resume=dict(question_id=qid,selected_option_id=choice_id(qid,reference(1)))),config)
    assert result['expert_result']['error_code']=='MAIL_SELECTION_MISMATCH'
    assert len(env.store.tool_calls)==1
