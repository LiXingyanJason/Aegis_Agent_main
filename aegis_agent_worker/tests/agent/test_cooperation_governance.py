"""8.5：治理组合回归，所有模型/工具/数据库均为本地替身。"""
import asyncio
from uuid import UUID

import pytest

from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.llm.client import LLMError
from aegis_agent_worker.repository.execution_repository import ExecutionRepository
from aegis_agent_worker.repository.run_repository import AgentRunRepository
from aegis_agent_worker.tool.contracts import ToolError
from aegis_agent_worker.service.calendar.calendar_confirmation_service import CalendarWriteError
from test_cooperation_runtime import RUN, DELEGATE_MAIL, Expert, setup, state
from test_harness_react_revision import Database, Result
from test_harness_layers import Database as AuditDatabase, Repository as AuditRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("budget",["model_calls","tool_calls","active_ms"])
async def test_delegation_checks_remaining_without_creating_a_new_account_or_call(budget):
    graph,harness,client,db,events,mail,calendar=setup([DELEGATE_MAIL])
    original=graph._agent.decide
    async def decide(**kwargs):
        result=await original(**kwargs)
        db.budgets[RUN.id][budget]={"model_calls":10,"tool_calls":6,"active_ms":120000}[budget]
        return result
    graph._agent.decide=decide
    async with harness.scope(RUN):
        with pytest.raises(HarnessError) as error: await graph.build().ainvoke(state())
    assert error.value.code=="EXECUTION_BUDGET_EXCEEDED"
    assert not mail.calls and not calendar.calls
    assert not any(item[1]["phase"]=="delegated" for item in events.items)
    assert events.items[-1][1]["error_code"]==error.value.code


@pytest.mark.asyncio
async def test_preflight_is_read_only_but_actual_reservation_rechecks_cancellation():
    db=Database()
    executor=HarnessExecutor(db)
    async with executor.scope(RUN):
        await executor.check_remaining()
        assert db.budgets[RUN.id]["model_calls"]==db.budgets[RUN.id]["tool_calls"]==0
        db.run_statuses[RUN.id]="cancelled"
        with pytest.raises(HarnessError) as error:
            async with executor.attempt("model","fake"): raise AssertionError("不得执行")
    assert error.value.code=="RUN_CANCELLED" and not db.attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("status",["completed","failed","waiting_input","waiting_confirmation"])
async def test_non_running_task_cannot_make_new_calls(status):
    db=Database(); executor=HarnessExecutor(db)
    async with executor.scope(RUN):
        db.run_statuses[RUN.id]=status
        with pytest.raises(HarnessError) as error:
            async with executor.attempt("model","fake"): raise AssertionError("不得执行")
    assert error.value.code=="EXECUTION_STATE_CHANGED" and not db.attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code",[
    (LLMError("network detail"),"LLM_UNAVAILABLE"),
    (TimeoutError(),"EXECUTION_TIMEOUT"),
    (asyncio.CancelledError(),"EXECUTION_CANCELLED"),
    (ToolError("MAIL_CONNECTION_REQUIRED","无权限"),"MAIL_CONNECTION_REQUIRED"),
])
async def test_attempt_audit_preserves_failure_class_without_raw_message(failure,code):
    repo=AuditRepository(); executor=HarnessExecutor(AuditDatabase(),repository=repo)
    async with executor.scope(RUN):
        with pytest.raises(BaseException):
            async with executor.attempt("model","fake"): raise failure
    assert repo.finished[-1]==dict(status="failed",error_code=code)


@pytest.mark.asyncio
async def test_summary_rewording_does_not_bypass_no_progress_limit():
    calls=0
    async def changed_summary(payload):
        nonlocal calls
        calls+=1
        return dict(status="unavailable",summary=f"暂不可用（第{calls}次说明）",references=[],draft=None,
                    error_code="MAIL_MEETING_UNAVAILABLE")
    graph,harness,client,db,events,mail,_=setup([DELEGATE_MAIL]*3,mail=Expert("mail",changed_summary))
    async with harness.scope(RUN): result=await graph.build().ainvoke(state())
    # 无新证据的失败专家重派在下一次工具/专家执行前停止，并保留原始错误。
    assert result["cooperation_error_code"]=="MAIL_MEETING_UNAVAILABLE" and len(mail.calls)==1
    assert events.items[-1][1]["error_code"]=="MAIL_MEETING_UNAVAILABLE"


@pytest.mark.asyncio
async def test_authority_and_cancelled_failure_sql_are_guarded_before_external_execution():
    statements=[]
    class Session:
        async def execute(self,sql,args): statements.append((str(sql),args)); return Result()
    repo=ExecutionRepository()
    await repo.snapshot(Session(),RUN)
    await repo.reserve_call(Session(),RUN,kind="tool",maximum=6,policy_version="harness-v1",elapsed_ms=0,time_limit_ms=120000)
    assert "ar.user_id=b.user_id" in statements[0][0]
    assert "ar.status='running'" in statements[1][0] and statements[1][1]["user"]==RUN.user_id
    statements.clear()
    changed=await AgentRunRepository().fail_run(Session(),run_id=RUN.id,tenant_id=RUN.tenant_id,
        step_id=UUID(int=111),error_code="LLM_UNAVAILABLE",error_message="不可用")
    assert changed is False and len(statements)==1
    assert "status = 'running' RETURNING id" in statements[0][0]


@pytest.mark.asyncio
async def test_cancellation_after_last_model_cannot_save_reply_or_completed_event():
    statements=[]
    class Session:
        async def execute(self,sql,args):
            sql=str(sql); statements.append(sql)
            if "FROM conversations" in sql: return Result(dict(id=RUN.conversation_id))
            if "SELECT status FROM agent_runs" in sql: return Result(dict(status="cancelled"))
            raise AssertionError("取消后不能写入回复")
    with pytest.raises(HarnessError) as error:
        await AgentRunRepository().complete_run_with_assistant_message(Session(),run_id=RUN.id,
            tenant_id=RUN.tenant_id,user_id=RUN.user_id,conversation_id=RUN.conversation_id,
            assistant_content="不应保存",step_id=None)
    assert error.value.code=="RUN_CANCELLED" and len(statements)==2


def test_final_write_error_preserves_code_in_existing_orchestrator_tool_error_path():
    error=CalendarWriteError("需先核对实际状态","CALENDAR_EXECUTION_UNCERTAIN")
    assert isinstance(error,ToolError) and error.code=="CALENDAR_EXECUTION_UNCERTAIN"


@pytest.mark.asyncio
@pytest.mark.parametrize("write,timeout,expected",[(False,False,2),(True,False,1),(False,True,1)])
async def test_gateway_read_retries_are_bounded_and_writes_or_timeouts_never_retry(write,timeout,expected):
    from aegis_agent_worker.tool.gateway import ToolGateway
    from aegis_agent_worker.tool.registry import ToolRegistry
    from aegis_agent_worker.tool.contracts import ToolDefinition,ToolInvocation
    name="calendar.create_event" if write else "mail.messages.list"
    calls=[]
    class Provider:
        async def invoke(self,context,arguments):
            calls.append(arguments)
            if timeout: raise TimeoutError()
            raise ToolError("TOOL_UNAVAILABLE","测试不可用")
    db=Database(); executor=HarnessExecutor(db)
    registry=ToolRegistry()
    registry.register(ToolDefinition(name,"1","write" if write else "read","test","受控工具"),Provider())
    gateway=ToolGateway(registry,harness=executor)
    args=dict(title="会议",start_at="2026-10-26T09:00:00+08:00",end_at="2026-10-26T09:50:00+08:00",
        attendees=[],idempotency_key="test-confirmed-001",timezone="Asia/Shanghai") if write else {}
    async with executor.scope(RUN):
        prepared=await gateway.prepare(None,tenant_id=RUN.tenant_id,user_id=RUN.user_id,run_id=RUN.id,
            invocation=ToolInvocation(name,args),allow_confirmed_write=write)
        with pytest.raises((ToolError,HarnessError)): await gateway.invoke(prepared)
    assert len(calls)==expected and db.budgets[RUN.id]["tool_calls"]==expected


@pytest.mark.asyncio
async def test_orchestrator_reports_cancelled_without_fail_overwrite_or_model(monkeypatch):
    from test_cooperation_integration import assemble
    app=assemble(monkeypatch,[])
    app.env.db.run_statuses[RUN.id]="cancelled"
    result=await app.orchestrator.execute(RUN.id)
    assert result.status=="cancelled" and result.error_code=="RUN_CANCELLED"
    assert not app.lifecycle.failed and not app.env.client.calls and not app.writes
