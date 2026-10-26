"""48 deterministic boundary observations; DB is a SQL-behaviour test double.

Real concurrent SQL is independently exercised by evaluate_harness_sql.py.
"""
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
from test_harness_react_revision import Database,RUN
from test_harness_layers import Database as AuditDatabase,Repository as AuditRepository
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.harness.policy import TaskExecutionPolicy
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
from aegis_agent_worker.llm.client import LLMMessage
from aegis_agent_worker.tool.contracts import ToolDefinition,ToolInvocation,ToolError,ToolResult
from aegis_agent_worker.tool.registry import ToolRegistry
from aegis_agent_worker.tool.gateway import ToolGateway


@pytest.mark.asyncio
@pytest.mark.parametrize('limit',[1,2,3,4])
async def test_model_limit(limit):
    db=Database();h=HarnessExecutor(db,replace(TaskExecutionPolicy(),max_models=limit));calls=[]
    async with h.scope(RUN):
        for _ in range(limit):
            async with h.attempt('model','fake'): calls.append(1)
        with pytest.raises(HarnessError) as error:
            async with h.attempt('model','fake'):calls.append(1)
    assert error.value.code=='EXECUTION_BUDGET_EXCEEDED' and len(calls)==limit
    assert db.budgets[RUN.id]['model_calls']==limit


@pytest.mark.asyncio
@pytest.mark.parametrize('limit',[1,2,3,4])
async def test_tool_limit(limit):
    db=Database();h=HarnessExecutor(db,replace(TaskExecutionPolicy(),max_tools=limit));calls=[]
    async with h.scope(RUN):
        for _ in range(limit):
            async with h.attempt('tool','mail.messages.get'):calls.append(1)
        with pytest.raises(HarnessError):
            async with h.attempt('tool','mail.messages.get'):calls.append(1)
    assert len(calls)==limit and db.budgets[RUN.id]['tool_calls']==limit


@pytest.mark.asyncio
@pytest.mark.parametrize('name',['shell.exec','mail.messages.send','calendar.create_event','knowledge.search'])
async def test_scope_denies_external_entry(name):
    db=Database();h=HarnessExecutor(db);calls=[]
    async with h.scope(RUN):
        with pytest.raises(HarnessError) as error:
            async with h.attempt('tool',name): calls.append(1)
    assert error.value.code=='TOOL_SCOPE_BLOCKED' and calls==[] and not db.attempts


@pytest.mark.asyncio
@pytest.mark.parametrize('key',['__tool_name','aegis_tenant_id','aegis_user_id','__connection'])
async def test_reserved_parameter_denied(key):
    registry=ToolRegistry();registry.register(ToolDefinition('mail.messages.list','1','read','mock','test'),None)
    with pytest.raises(ToolError) as error:
        await ToolGateway(registry).prepare(None,tenant_id=RUN.tenant_id,user_id=RUN.user_id,run_id=RUN.id,
            invocation=ToolInvocation('mail.messages.list',{key:'forged'}))
    assert error.value.code=='TOOL_INVALID_ARGUMENTS'


@pytest.mark.asyncio
@pytest.mark.parametrize('name,approved',[(n,a) for n in ('calendar.create_event','mail.messages.send') for a in (False,True)])
async def test_write_gate(name,approved):
    calls=[]
    class Handler:
        async def invoke(self,*args):calls.append(1);return ToolResult({}, {})
    registry=ToolRegistry();registry.register(ToolDefinition(name,'1','write','mock','test'),Handler())
    gateway=ToolGateway(registry)
    args=(dict(to=['a@example.com'],subject='测试',body='测试',idempotency_key='e08') if name.startswith('mail') else
        dict(title='测试',start_at='2026-11-02T10:00:00+08:00',end_at='2026-11-02T10:30:00+08:00',attendees=[],idempotency_key='e08',timezone='Asia/Shanghai'))
    if approved:
        prepared=await gateway.prepare(None,tenant_id=RUN.tenant_id,user_id=RUN.user_id,run_id=RUN.id,invocation=ToolInvocation(name,args),allow_confirmed_write=True)
        await gateway.invoke(prepared)
        assert len(calls)==1
    else:
        with pytest.raises(ToolError) as error:
            await gateway.prepare(None,tenant_id=RUN.tenant_id,user_id=RUN.user_id,run_id=RUN.id,invocation=ToolInvocation(name,args))
        assert error.value.code=='TOOL_POLICY_BLOCKED' and calls==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('args',[{'tenant_id':'other'},{'limit':-1},{'action':'send'},{'unexpected':'x'}])
async def test_invalid_schema(args):
    registry=ToolRegistry();registry.register(ToolDefinition('mail.messages.list','1','read','mock','test'),None)
    with pytest.raises(ToolError) as error:
        await ToolGateway(registry).prepare(None,tenant_id=RUN.tenant_id,user_id=RUN.user_id,run_id=RUN.id,invocation=ToolInvocation('mail.messages.list',args))
    assert error.value.code=='TOOL_INVALID_ARGUMENTS'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,timeout',[('model',.001),('model',.002),('tool',.001),('tool',.002)])
async def test_timeout_audit_and_cancellation(kind,timeout):
    class Policy(TaskExecutionPolicy):
        def call_timeout(self,kind):return timeout
    repo=AuditRepository();h=HarnessExecutor(AuditDatabase(),Policy(),repo);cancelled=[]
    async with h.scope(RUN):
        with pytest.raises(HarnessError) as error:
            async with h.attempt(kind,'mail.messages.get' if kind=='tool' else 'fake'):
                try:await asyncio.Event().wait()
                finally:cancelled.append(1)
    assert cancelled==[1] and error.value.code=='EXECUTION_TIMEOUT'
    assert repo.finished[-1]['status']=='failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('size',[29,30,31,60])
async def test_input_boundary(size):
    calls=[]
    class Raw:
        provider_name=model_name='fake'
        async def complete(self,messages):calls.append(1);return 'ok'
    repo=AuditRepository();h=HarnessExecutor(AuditDatabase(),replace(TaskExecutionPolicy(),max_input_chars=30),repo)
    client=GuardedLLMClient(Raw(),h)
    async with h.scope(RUN):
        if size<=30:assert await client.complete([LLMMessage('user','x'*size)])=='ok'
        else:
            with pytest.raises(HarnessError) as error:await client.complete([LLMMessage('user','x'*size)])
            assert error.value.code=='MODEL_INPUT_TOO_LARGE'
    assert len(calls)==int(size<=30)


@pytest.mark.asyncio
@pytest.mark.parametrize('size',[1,2,3,5])
async def test_output_boundary(size):
    calls=[]
    class Raw:
        provider_name=model_name='fake'
        async def complete(self,messages):calls.append(1);return 'x'*size
    repo=AuditRepository();h=HarnessExecutor(AuditDatabase(),replace(TaskExecutionPolicy(),max_output_chars=2),repo)
    client=GuardedLLMClient(Raw(),h)
    async with h.scope(RUN):
        if size<=2: assert await client.complete([LLMMessage('user','test')])=='x'*size
        else:
            with pytest.raises(HarnessError) as error:await client.complete([LLMMessage('user','test')])
            assert error.value.code=='MODEL_OUTPUT_TOO_LARGE'
    assert len(calls)==1 and repo.finished[-1]['status']==('succeeded' if size<=2 else 'failed')


@pytest.mark.asyncio
@pytest.mark.parametrize('status',['cancelled','waiting_input','waiting_confirmation','completed'])
async def test_nonrunning_task(status):
    db=Database();h=HarnessExecutor(db);calls=[]
    async with h.scope(RUN):
        db.run_statuses[RUN.id]=status
        with pytest.raises(HarnessError):
            async with h.attempt('model','fake'):calls.append(1)
    assert calls==[] and not db.attempts


@pytest.mark.asyncio
@pytest.mark.parametrize('limit',[1,2,3,4])
async def test_resume_does_not_reset_budget(limit):
    db=Database();h=HarnessExecutor(db,replace(TaskExecutionPolicy(),max_models=limit));calls=[]
    for _ in range(limit):
        async with h.scope(RUN):
            async with h.attempt('model','fake'):calls.append(1)
    async with h.scope(RUN):
        with pytest.raises(HarnessError):
            async with h.attempt('model','fake'):calls.append(1)
    assert len(calls)==limit and db.budgets[RUN.id]['model_calls']==limit


@pytest.mark.asyncio
@pytest.mark.parametrize('code',['LLM_UNAVAILABLE','MODEL_OUTPUT_INVALID','EXECUTION_CANCELLED','TEST_FAILURE'])
async def test_stable_error_audit(code):
    repo=AuditRepository();h=HarnessExecutor(AuditDatabase(),repository=repo)
    async with h.scope(RUN):
        with pytest.raises(HarnessError):
            async with h.attempt('model','fake'):raise HarnessError(code,'sensitive body should not be in audit')
    assert repo.finished[-1]==dict(status='failed',error_code=code)
