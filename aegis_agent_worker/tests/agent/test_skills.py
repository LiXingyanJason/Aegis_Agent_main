import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aegis_agent_worker.agent.skills.contracts import SkillContent,SkillReadRequest
from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.harness.policy_provider import LEGACY_POLICY,PolicyProvider,with_skills
from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
from aegis_agent_worker.llm.client import LLMMessage
from aegis_agent_worker.llm.usage import model_call_purpose
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.service.skills.skill_context_service import SkillContextService
from aegis_agent_worker.tool.contracts import ToolDefinition,ToolInvocation,ToolError
from aegis_agent_worker.tool.gateway import ToolGateway
from aegis_agent_worker.tool.registry import ToolRegistry

class Database:
    @asynccontextmanager
    async def session(self): yield SimpleNamespace(begin=self.begin,execute=AsyncMock())
    @asynccontextmanager
    async def begin(self): yield

class Execution:
    def __init__(self): self.calls=[];self.audits=[];self.maximum=None
    async def ensure_budget(self,s,r,v): return v
    async def add_active_time(self,*a): pass
    async def reserve_call(self,*a,**kw):
        if self.maximum is not None and len(self.calls)>=self.maximum:
            raise HarnessError('EXECUTION_BUDGET_EXCEEDED','budget')
        self.calls.append(kw);return 0
    async def start_attempt(self,*a,**kw): return uuid4()
    async def finish_attempt(self,s,id,**kw): self.audits.append((id,kw))

class Repository:
    def __init__(self):
        content=SkillContent(name='renewal-reply',description='续约草稿',body='回应事项，再列待确认问题。',purposes=['mail_reply_draft','mail_draft_revision'])
        self.row={**content.model_dump(),'skill_id':uuid4(),'version':1,'content_hash':content.digest(),'pinned_hash':content.digest(),'reads':0}
        self.binding=None;self.records=[];self.enabled=True;self.catalogue=True
    @asynccontextmanager
    async def session(self,run): yield None
    async def claim_selection(self,run,purpose):
        if self.binding is None:
            self.binding={'status':'selecting'};return True,self.binding
        return False,self.binding
    async def candidates(self,*args):
        return [dict(id=str(self.row['skill_id']),version=1,name='renewal-reply',description='续约草稿',content_hash=self.row['content_hash'])] if self.catalogue else []
    async def select(self,run,purpose,selected,reason): self.binding={'status':'selected' if selected else 'none','reason':reason}
    async def claim_read(self,*args):
        if self.row['reads']: return False
        self.row['reads']=1;return True
    async def load(self,*args): return dict(self.row) if self.enabled else None
    async def record(self,run,purpose,attempt,row): self.records.append((purpose,attempt,row['version']))

def setup(profile=None):
    profile=profile or with_skills(LEGACY_POLICY)
    repo,execution=Repository(),Execution()
    service=SkillContextService(repo)
    harness=HarnessExecutor(Database(),repository=execution,policy_provider=PolicyProvider((profile,),profile.version))
    registry=ToolRegistry()
    registry.register(ToolDefinition('skills.read','1.0','read','internal','Skill',argument_schema=SkillReadRequest),service)
    service.gateway=ToolGateway(registry,harness=harness)
    raw=SimpleNamespace(model_name='fake',provider_name='fake',complete=AsyncMock())
    raw.complete_json=raw.complete
    raw.complete.side_effect=[json.dumps({'skill_id':str(repo.row['skill_id'])}),'answer']
    client=GuardedLLMClient(raw,harness,skill_context=service)
    run=ClaimedRun(uuid4(),uuid4(),uuid4(),None,'mail_reply_draft')
    return run,client,raw,service,repo,harness,execution

@pytest.mark.asyncio
async def test_read_then_inject_and_resume_same_version_with_per_call_audit():
    run,client,raw,service,repo,harness,execution=setup()
    raw.complete.side_effect=[json.dumps({'skill_id':str(repo.row['skill_id'])}),'first','second']
    for _ in range(2):
        async with harness.scope(run):
            with model_call_purpose('mail_reply_draft'):
                await client.complete_json([LLMMessage('system','base'),LLMMessage('user','本轮要求')])
    assert [call['kind'] for call in execution.calls]==['model','tool','model','model']
    assert repo.row['reads']==1 and len(repo.records)==2
    assert len({record[1] for record in repo.records})==2
    assert 'guide' not in str(raw.complete.call_args_list[0])
    messages=raw.complete.call_args_list[-1].args[0]
    assert messages[0].content=='base' and messages[1].role=='user' and messages[-1].content=='本轮要求'
    assert 'guide' in messages[1].content
    assert all(audit['total_tokens'] is None for _,audit in execution.audits if audit.get('purpose'))

@pytest.mark.asyncio
@pytest.mark.parametrize('purpose',['main_routing','mail_extraction','mail_collection','knowledge_answer','calendar_planning','cooperation_routing','skill_selection'])
async def test_disallowed_purpose_never_selects_or_reads(purpose):
    run,client,raw,service,repo,harness,execution=setup()
    raw.complete.side_effect=['ok']
    async with harness.scope(run):
        with model_call_purpose(purpose): await client.complete([LLMMessage('user','任务')])
    assert repo.binding is None and repo.row['reads']==0 and len(execution.calls)==1

@pytest.mark.asyncio
@pytest.mark.parametrize('selection',['plain text','{}','{"skill_id":null}',json.dumps({'skill_id':str(uuid4())})])
async def test_invalid_or_none_selection_never_reads(selection):
    run,client,raw,service,repo,harness,execution=setup()
    raw.complete.side_effect=[selection,'ok']
    async with harness.scope(run):
        with model_call_purpose('mail_reply_draft'): await client.complete([LLMMessage('user','任务')])
    assert repo.binding['status']=='none' and repo.row['reads']==0 and not repo.records
    assert len(execution.calls)==2

@pytest.mark.asyncio
@pytest.mark.parametrize('changed',['disabled','hash'])
async def test_revocation_and_tampered_version_stop_before_next_model_call(changed):
    run,client,raw,service,repo,harness,execution=setup()
    async with harness.scope(run):
        with model_call_purpose('mail_reply_draft'):
            await client.complete([LLMMessage('user','任务')])
            if changed=='disabled': repo.enabled=False
            else: repo.row['body']='changed'
            with pytest.raises(HarnessError): await client.complete([LLMMessage('user','继续')])
    assert raw.complete.await_count==2 and len(repo.records)==1

@pytest.mark.asyncio
async def test_budget_failure_not_retried_as_format_and_does_not_read():
    run,client,raw,service,repo,harness,execution=setup()
    execution.maximum=0
    async with harness.scope(run):
        with model_call_purpose('mail_reply_draft'):
            with pytest.raises(HarnessError,match='budget'): await client.complete([LLMMessage('user','任务')])
    assert raw.complete.await_count==0 and repo.row['reads']==0

@pytest.mark.asyncio
async def test_no_catalogue_and_old_policy_add_no_model_calls():
    for old in (False,True):
        run,client,raw,service,repo,harness,execution=setup(LEGACY_POLICY if old else None)
        repo.catalogue=False;raw.complete.side_effect=['ok']
        async with harness.scope(run):
            with model_call_purpose('mail_reply_draft'): await client.complete([LLMMessage('user','任务')])
        assert len(execution.calls)==1 and not repo.records

@pytest.mark.asyncio
async def test_gateway_rejects_paths_and_second_read():
    run,client,raw,service,repo,harness,execution=setup()
    async with harness.scope(run):
        with pytest.raises(ToolError):
            await service.gateway.prepare(None,tenant_id=run.tenant_id,user_id=run.user_id,run_id=run.id,
                invocation=ToolInvocation('skills.read',{'skill_id':'../../x','purpose':'mail_reply_draft','path':'x'}))
        with model_call_purpose('mail_reply_draft'): await client.complete([LLMMessage('user','任务')])
        prepared=await service.gateway.prepare(None,tenant_id=run.tenant_id,user_id=run.user_id,run_id=run.id,
            invocation=ToolInvocation('skills.read',{'skill_id':str(repo.row['skill_id']),'purpose':'mail_reply_draft'}))
        with pytest.raises(ToolError,match='次数'): await service.gateway.invoke(prepared)

@pytest.mark.asyncio
async def test_mail_repair_reuses_guide_but_audits_both_model_attempts():
    run,client,raw,service,repo,harness,execution=setup()
    raw.complete.side_effect=[json.dumps({'skill_id':str(repo.row['skill_id'])}),'invalid',json.dumps({'body':'新正文'})]
    run=replace(run,run_type='mail_draft_revision')
    async with harness.scope(run):
        assert await MailAgent(client).revise_body('正文','正式一点')=='新正文'
    assert repo.row['reads']==1 and len(repo.records)==2 and len(execution.calls)==4

def test_content_size_unknown_purpose_and_extra_authority_are_rejected():
    base=dict(name='x',description='x',body='x',purposes=['mail_reply_draft'])
    for change in ({'body':'x'*6001},{'purposes':['main_routing']},{'allowed_tools':['mail.messages.send']}):
        with pytest.raises(ValueError): SkillContent(**{**base,**change})

def test_selection_summary_keeps_instruction_before_long_mail():
    from aegis_agent_worker.agent.skills.selector import selection_task
    payload={'mail':{'subject':'续约','body_text':'邮件正文'*4000},'instruction':'只确认收到，不承诺续约'}
    summary=selection_task([LLMMessage('user',json.dumps(payload,ensure_ascii=False))],4000)
    assert len(summary)<=4000 and json.loads(summary)['instruction']==payload['instruction']
