"""7.4真实调用边界：用途、预算、修复/恢复重新读取与无正文审计。"""
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
from aegis_agent_worker.common.usage import TokenUsage
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.harness.policy_provider import LEGACY_POLICY, PolicyProvider
from aegis_agent_worker.llm.client import LLMMessage
from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
from aegis_agent_worker.llm.memory_scope import memory_call_scope,current_memory_request
from aegis_agent_worker.llm.usage import model_call_purpose
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.service.memory.memory_context_service import MemoryContextService


class Database:
    @asynccontextmanager
    async def session(self):
        yield SimpleNamespace(begin=self.begin,execute=AsyncMock())
    @asynccontextmanager
    async def begin(self):
        yield


class Execution:
    def __init__(self): self.finished=[]
    async def ensure_budget(self,session,run,version): return version
    async def add_active_time(self,*args): pass
    async def reserve_call(self,*args,**kwargs): return 0
    async def start_attempt(self,*args,**kwargs): return uuid4()
    async def finish_attempt(self,session,attempt_id,**kwargs): self.finished.append((attempt_id,kwargs))


def row(content='邮件语气正式。',**changes):
    return dict(id=uuid4(),content=content,source='user_confirmed',memory_type='long_term_summary',
                version=2,source_kind='manual',source_object_id=None,source_version=None,**changes)


class Memories:
    def __init__(self): self.rows=[row()];self.reads=0;self.usages=[];self.tokens=[]
    async def list_active_non_sensitive(self,*args,**kwargs):
        self.reads+=1
        return self.rows.copy()
    async def record_bundle_usage(self,session,*,run,attempt_id,bundle):
        self.usages.append((run.id,attempt_id,bundle))
    async def finish_bundle_usage(self,session,*,run,attempt_id,usage): self.tokens.append((attempt_id,usage))


def setup(profile=LEGACY_POLICY,conversation=True):
    run=ClaimedRun(id=uuid4(),tenant_id=uuid4(),user_id=uuid4(),
        conversation_id=uuid4() if conversation else None,run_type='conversation' if conversation else 'mail_reply_draft')
    db,execution,memories=Database(),Execution(),Memories()
    policies=PolicyProvider((profile,),profile.version)
    service=MemoryContextService(db,memories,policy_provider=policies)
    harness=HarnessExecutor(db,repository=execution,policy_provider=policies)
    client=SimpleNamespace(provider_name='test',model_name='test',usage=TokenUsage(),
        complete=AsyncMock(return_value='ok'))
    return run,service,harness,client,memories,execution


@pytest.mark.asyncio
@pytest.mark.parametrize('purpose',['conversation_reply','mail_reply_draft','mail_draft_revision'])
async def test_allowed_calls_have_attempt_scoped_bundle_and_unknown_token_usage(purpose):
    run,service,harness,client,memories,execution=setup(conversation=False)
    with memory_call_scope(service,run,purpose,'起草邮件'):
        async with harness.scope(run):
            await GuardedLLMClient(client,harness).complete([LLMMessage(role='system',content='边界'),LLMMessage(role='user',content='当前要求')])
    assert current_memory_request() is None and memories.reads==1
    audit=execution.finished[0]
    assert memories.usages[0][1]==audit[0]
    bundle=memories.usages[0][2]
    assert bundle.purpose==purpose and bundle.conversation_id is None and bundle.items[0].version==2
    assert bundle.estimated_tokens is None and audit[1]['prompt_tokens'] is None
    assert audit[1]['memory_count']==1 and audit[1]['purpose']==purpose
    assert audit[1]['memory_selection_details']['ranking_policy_version']=='memory-rank-v1'
    assert audit[1]['memory_selection_details']['candidates_considered']==1
    assert '邮件语气正式' not in str(audit)
    messages=client.complete.await_args.args[0]
    assert messages[0].role=='system' and messages[1].role=='user' and messages[-1].content=='当前要求'
    assert 'memory_data' in messages[1].content and '邮件语气' not in messages[1].content # JSON转义隔离数据
    assert memories.tokens[0][1].prompt_tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize('purpose',['main_routing','main_routing_repair','mail_extraction','knowledge_answer',
                                   'knowledge_planning','mail_collection','model_json'])
async def test_forbidden_purposes_never_read_or_inject_even_in_outer_draft_scope(purpose):
    run,service,harness,client,memories,execution=setup()
    with memory_call_scope(service,run,'mail_reply_draft','邮件'):
        with model_call_purpose(purpose):
            async with harness.scope(run):
                await GuardedLLMClient(client,harness).complete([LLMMessage(role='user',content='任务')])
    assert memories.reads==0 and not memories.usages
    assert len(client.complete.await_args.args[0])==1 and execution.finished[0][1]['memory_count']==0


@pytest.mark.asyncio
async def test_repair_rechecks_deletion_and_audits_each_actual_attempt():
    run,service,harness,client,memories,execution=setup()
    async def complete(messages):
        if memories.rows:
            memories.rows=[] # 首次请求后用户删除了记忆；格式修复前不能复用旧摘录。
            return 'not json'
        return '{"to":["a@example.com"],"cc":[],"subject":"回复","body":"正文"}'
    client.complete.side_effect=complete
    with memory_call_scope(service,run,'mail_reply_draft','邮件回复'):
        async with harness.scope(run):
            await MailAgent(GuardedLLMClient(client,harness)).generate_reply_draft(
                {'sender_email':'a@example.com'},key_points=[],instruction='正式')
    assert memories.reads==2 and len(memories.usages)==1
    assert [item[1]['memory_count'] for item in execution.finished]==[1,0]
    assert 'memory_data' not in str(client.complete.await_args_list[1].args)


@pytest.mark.asyncio
async def test_supplier_usage_is_whole_call_not_per_memory_cost():
    run,service,harness,client,memories,execution=setup()
    client.usage=TokenUsage(prompt_tokens=123,completion_tokens=45,total_tokens=168)
    with memory_call_scope(service,run,'conversation_reply','邮件'):
        async with harness.scope(run):
            await GuardedLLMClient(client,harness).complete([LLMMessage(role='user',content='邮件')])
    assert memories.tokens[0][1].total_tokens==168
    assert execution.finished[0][1]['prompt_tokens']==123
    assert memories.usages[0][2].estimated_tokens is None


@pytest.mark.asyncio
async def test_limits_and_unavailable_memory_fail_without_personalization():
    profile=replace(LEGACY_POLICY,memory=replace(LEGACY_POLICY.memory,max_items=1,max_total_chars=400))
    run,service,_,_,memories,_=setup(profile)
    memories.rows=[row(),row('邮件事项先核对联系人。')]
    bundle,reason=await service.prepare_call(run,purpose='mail_reply_draft',task_text='邮件',remaining_chars=10000)
    assert len(bundle.items)==1
    bundle,reason=await service.prepare_call(run,purpose='mail_reply_draft',task_text='邮件',remaining_chars=1)
    assert not bundle.items and reason=='memory_budget_exhausted'
    service._repository.list_active_non_sensitive=AsyncMock(side_effect=OperationalError('SELECT',{},Exception('offline')))
    bundle,reason=await service.prepare_call(run,purpose='mail_reply_draft',task_text='邮件',remaining_chars=10000)
    assert not bundle.items and reason=='memory_unavailable'


@pytest.mark.asyncio
async def test_unknown_token_estimate_denies_when_hard_token_cap_configured():
    profile=replace(LEGACY_POLICY,memory=replace(LEGACY_POLICY.memory,max_estimated_tokens=100))
    run,service,_,_,memories,_=setup(profile)
    bundle,reason=await service.prepare_call(run,purpose='conversation_reply',task_text='邮件',remaining_chars=10000)
    assert not bundle.items and reason=='token_estimate_unknown' and memories.reads==0


@pytest.mark.asyncio
async def test_audit_failure_blocks_supplier_request():
    run,service,harness,client,memories,_=setup()
    memories.record_bundle_usage=AsyncMock(side_effect=RuntimeError('audit unavailable'))
    with memory_call_scope(service,run,'mail_reply_draft','邮件'):
        async with harness.scope(run):
            with pytest.raises(RuntimeError):
                await GuardedLLMClient(client,harness).complete([LLMMessage(role='user',content='邮件')])
    client.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_deleted_memory_not_reused_after_langgraph_interrupt_resume():
    from typing import TypedDict
    from langgraph.graph import StateGraph,START,END
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import interrupt,Command
    class State(TypedDict):
        memory_context:str
        answer:str
    run,service,harness,client,memories,execution=setup()
    async def generate(state):
        with memory_call_scope(service,run,'mail_reply_draft','邮件回复'):
            await GuardedLLMClient(client,harness).complete([LLMMessage(role='user',content='邮件回复')])
        return {}
    def wait(state):
        return {'answer':interrupt({'need_input':True})}
    graph=StateGraph(State)
    graph.add_node('first',generate);graph.add_node('wait',wait);graph.add_node('after_resume',generate)
    graph.add_edge(START,'first');graph.add_edge('first','wait');graph.add_edge('wait','after_resume');graph.add_edge('after_resume',END)
    compiled=graph.compile(checkpointer=MemorySaver())
    config={'configurable':{'thread_id':str(run.id)}}
    async with harness.scope(run):
        result=await compiled.ainvoke({'memory_context':'已删除的旧Checkpoint摘录'},config)
    assert result.get('__interrupt__') and memories.reads==1
    memories.rows=[]
    async with harness.scope(run):
        await compiled.ainvoke(Command(resume='补充回答'),config)
    assert memories.reads==2 and len(memories.usages)==1
    assert [item[1]['memory_count'] for item in execution.finished]==[1,0]
    assert 'memory_data' not in str(client.complete.await_args_list[-1].args)
    assert 'Checkpoint' not in str(client.complete.await_args_list[-1].args)


@pytest.mark.asyncio
async def test_execution_budget_rejection_happens_before_memory_or_provider_access():
    from aegis_agent_worker.harness.exceptions import HarnessError
    run,service,harness,client,memories,execution=setup()
    execution.reserve_call=AsyncMock(return_value=None)
    with memory_call_scope(service,run,'conversation_reply','邮件'):
        async with harness.scope(run):
            with pytest.raises(HarnessError) as failure:
                await GuardedLLMClient(client,harness).complete([LLMMessage(role='user',content='邮件')])
    assert failure.value.code=='EXECUTION_BUDGET_EXCEEDED'
    assert memories.reads==0 and not memories.usages
    client.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_identity_mismatch_never_reads_other_users_memory():
    from aegis_agent_worker.harness.exceptions import HarnessError
    run,service,harness,client,memories,execution=setup()
    impostor=replace(run,user_id=uuid4())
    with memory_call_scope(service,impostor,'conversation_reply','邮件'):
        async with harness.scope(run):
            with pytest.raises(HarnessError) as failure:
                await GuardedLLMClient(client,harness).complete([LLMMessage(role='user',content='邮件')])
    assert failure.value.code=='MEMORY_IDENTITY_MISMATCH'
    assert memories.reads==0
    client.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_entity_validated_and_char_byte_limits_shared_by_service():
    profile=replace(LEGACY_POLICY,memory=replace(LEGACY_POLICY.memory,max_items=2,max_item_chars=400,
        max_total_chars=400,max_content_bytes=30))
    run,service,_,_,memories,_=setup(profile)
    memories.rows=[row('邮件'+'长'*500),row('邮件简洁。')]
    bundle,_=await service.prepare_call(run,purpose='mail_reply_draft',task_text='邮件',remaining_chars=10000)
    assert len(bundle.items)==1 and bundle.items[0].excerpt=='邮件简洁。'
    assert bundle.content_bytes<=30
    memories.rows=[{**row(),'memory_type':'entity','entity_value':{'key':'contact','name':'李莉','email':'not-valid'}}]
    bundle,_=await service.prepare_call(run,purpose='mail_reply_draft',task_text='邮件李莉',remaining_chars=10000)
    assert not bundle.items
