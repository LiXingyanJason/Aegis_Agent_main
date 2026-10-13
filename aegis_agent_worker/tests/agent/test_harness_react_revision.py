"""预算、受限信息收集、输入恢复和草稿版本保护回归。"""
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID,uuid4
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
from aegis_agent_worker.harness.policy import TaskExecutionPolicy
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.agent.workflows.mail.collection_workflow import MailCollectionWorkflow
from aegis_agent_worker.agent.contracts.react import ReActDecision,RevisedBody
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.repository.mail_revision_repository import MailRevisionRepository
from aegis_agent_worker.tool.gateway import ToolGateway
from aegis_agent_worker.tool.registry import ToolRegistry
from aegis_agent_worker.tool.contracts import ToolDefinition,ToolInvocation
from aegis_agent_worker.observability.trace_context import serialize_run_message,deserialize_run_message

RUN=ClaimedRun(id=UUID(int=1),tenant_id=UUID(int=2),user_id=UUID(int=3),conversation_id=None,run_type="mail_reply_draft")


class Result:
    def __init__(self,row=None): self.row=row
    def mappings(self): return self
    def first(self): return self.row
    def one(self): assert self.row is not None; return self.row


class Database:
    def __init__(self): self.budgets={}; self.attempts=[]
    @asynccontextmanager
    async def session(self): yield Session(self)


class Session:
    def __init__(self,database): self.db=database
    @asynccontextmanager
    async def begin(self): yield
    async def execute(self,sql,args):
        sql=str(sql)
        if "INSERT INTO run_execution_budgets" in sql:
            self.db.budgets.setdefault(args["id"],dict(model_calls=0,tool_calls=0,active_ms=0))
        elif "UPDATE run_execution_budgets" in sql:
            row=self.db.budgets[args["id"]]
            if "maximum" in args:
                field="model_calls" if "SET model_calls" in sql else "tool_calls"
                if row[field]>=args["maximum"] or row["active_ms"]+args["elapsed"]>=args["time_limit"]:
                    return Result()
                row[field]+=1
                return Result({"active_ms":row["active_ms"]})
            row["active_ms"]+=args["elapsed"]
        elif "INSERT INTO run_execution_attempts" in sql:
            self.db.attempts.append(args)
            return Result({"id":uuid4()})
        return Result()


@pytest.mark.asyncio
async def test_budget_survives_resume_and_blocks_calls_before_execution():
    """同一任务的新消费轮次不重置计数，超出预算不触发模型或网络。"""
    db=Database()
    harness=HarnessExecutor(db,replace(TaskExecutionPolicy(),max_models=1))
    class Client:
        provider_name=model_name="test"
        calls=0
        async def complete(self,messages): self.calls+=1; return "ok"
    client=Client()
    wrapped=GuardedLLMClient(client,harness)
    async with harness.scope(RUN):
        assert await wrapped.complete([SimpleNamespace(role="user",content="task")])=="ok"
    async with harness.scope(RUN):
        with pytest.raises(HarnessError,match="预算"):
            await wrapped.complete([SimpleNamespace(role="user",content="task")])
        with pytest.raises(HarnessError):
            async with harness.attempt("tool","mail.messages.send"): pass
    assert client.calls==1 and db.budgets[RUN.id]["model_calls"]==1


@pytest.mark.asyncio
async def test_json_repair_cannot_bypass_persisted_model_budget():
    """JSON首次输出不合法后，修复仍占模型预算；预算不足时不发送第二次请求。"""
    from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
    db = Database()
    harness = HarnessExecutor(db, replace(TaskExecutionPolicy(), max_models=1))
    class Client:
        provider_name = model_name = "test"
        calls = 0
        async def complete(self, messages):
            raise AssertionError("必须走 JSON mode")
        async def complete_json(self, messages):
            self.calls += 1
            return "非法文字响应"
    client = Client()
    async with harness.scope(RUN):
        with pytest.raises(HarnessError) as error:
            await MailAgent(GuardedLLMClient(client, harness)).collect_information(
                source={}, evidence=[], answers=[],
            )
    assert error.value.code == "EXECUTION_BUDGET_EXCEEDED" and client.calls == 1


@pytest.mark.asyncio
async def test_gateway_rejects_reserved_and_unknown_arguments_before_connection_lookup():
    """模型不能覆盖内部tool_name或伪造身份，参数错误在外部账户查询前被阻断。"""
    registry=ToolRegistry()
    registry.register(ToolDefinition(name="mail.messages.list",version="1",risk_level="read",
                                     mcp_server="email",description="列表"),None)
    gateway=ToolGateway(registry)
    for args in ({"__tool_name":"mail.messages.send"},{"tenant_id":"other"}):
        with pytest.raises(Exception):
            await gateway.prepare(None,tenant_id=RUN.tenant_id,user_id=RUN.user_id,run_id=RUN.id,
                                  invocation=ToolInvocation("mail.messages.list",args))


class Tasks:
    async def load_reply_source(self,run):
        return dict(source_version="v1",provider_message_id="original",connection_id=UUID(int=10),
                    subject="续约",instruction="结合下周可用时间",key_points=[])


class Inputs:
    calls=0
    async def prepare(self,run,question):
        self.calls+=1
        return dict(question_id=str(UUID(int=20)),version=1,question=question)


@pytest.mark.asyncio
async def test_collection_interrupt_and_resume_keep_question_and_round_budget():
    """补充问答恢复同一检查点，不重复创建问题，不把回答解释成批准。"""
    class Agent:
        calls=0
        async def collect_information(self,**kwargs):
            self.calls+=1
            return ReActDecision(action="ask_user",question="续约报价是多少？") if self.calls==1 else ReActDecision(action="generate_draft")
    inputs=Inputs(); agent=Agent()
    graph=MailCollectionWorkflow(agent,Tasks(),None,inputs).build()
    from langgraph.graph import StateGraph,START,END
    from aegis_agent_worker.agent.graph.state import TaskGraphState
    root=StateGraph(TaskGraphState); root.add_node("collect",graph)
    root.add_edge(START,"collect"); root.add_edge("collect",END)
    graph=root.compile(checkpointer=MemorySaver())
    state=dict(run_id=str(RUN.id),tenant_id=str(RUN.tenant_id),user_id=str(RUN.user_id),
               conversation_id="",run_type="mail_reply_draft",
               mail_decision={"target":{"source_version":"v1"}})
    config={"configurable":{"thread_id":str(RUN.id)}}
    first=await graph.ainvoke(state,config)
    assert first["__interrupt__"][0].value["question_id"]==str(UUID(int=20))
    final=await graph.ainvoke(Command(resume={"question_id":str(UUID(int=20)),"response":"报价100元"}),config)
    assert final["user_answers"]==["报价100元"] and not final["waiting_input"]
    assert inputs.calls==1 and final["collection_round"]==2


@pytest.mark.asyncio
async def test_collection_unknown_reference_and_loop_limit_stop_execution():
    """未提供的邮件引用和过多决策轮次不能绕过执行入口。"""
    class Agent:
        async def collect_information(self,**kwargs):
            return ReActDecision(action="call_tool",tool_name="mail.messages.get",arguments={"provider_message_id":"unknown"})
    graph=MailCollectionWorkflow(Agent(),Tasks(),None,None).build()
    state=dict(run_id=str(RUN.id),tenant_id=str(RUN.tenant_id),user_id=str(RUN.user_id),
               run_type="mail_reply_draft",mail_decision={"target":{"source_version":"v1"}})
    with pytest.raises(HarnessError):
        await graph.ainvoke(state)
    with pytest.raises(HarnessError):
        await graph.ainvoke({**state,"collection_round":5})


@pytest.mark.asyncio
async def test_revision_conflict_does_not_write_other_fields_or_complete_run():
    """AI结果回写时版本改变，不能覆盖用户编辑或修改收件人／主题。"""
    class ConflictSession:
        calls=[]
        async def execute(self,sql,args): self.calls.append((str(sql),args)); return Result()
    session=ConflictSession()
    with pytest.raises(HarnessError):
        await MailRevisionRepository().save(session,RUN,{"id":UUID(int=8),"version":1},"new body")
    assert len(session.calls)==1
    sql,args=session.calls[0]
    assert "version=:version" in sql and "status='draft'" in sql
    assert "subject=" not in sql and "recipients" not in sql


def test_input_queue_protocol_and_content_contracts():
    """输入恢复只携带问题ID，与发送批准不同；模型不能输出发送或收件人修改。"""
    value=deserialize_run_message(serialize_run_message(RUN.id,resume_input_id=UUID(int=20)))
    assert value.resume_input_id==UUID(int=20) and value.resume_decision is None
    with pytest.raises(ValueError):
        serialize_run_message(RUN.id,resume_decision="approved",resume_input_id=UUID(int=20))
    with pytest.raises(ValueError):
        ReActDecision(action="call_tool",tool_name="mail.messages.send")
    with pytest.raises(ValueError):
        RevisedBody(body="正文",to=["attacker@example.com"])


@pytest.mark.asyncio
async def test_changed_evidence_after_user_answer_blocks_draft():
    """用户等待期间日历事实变化，恢复节点拒绝使用旧时段继续起草。"""
    class Tools:
        async def invoke(self, run, decision, connection):
            return {"available_slots": []}
    workflow = MailCollectionWorkflow(None, Tasks(), Tools(), None)
    state = dict(run_id=str(RUN.id), tenant_id=str(RUN.tenant_id),
                 user_id=str(RUN.user_id), run_type="mail_reply_draft",
                 collected_evidence=[{
                     "tool_name": "calendar.list_events",
                     "arguments": {"start_at": "2026-10-19T00:00:00+08:00",
                                   "end_at": "2026-10-20T00:00:00+08:00"},
                     "source_hash": workflow._hash({"available_slots": [{"id": "old"}]}),
                     "data": {},
                 }])
    with pytest.raises(HarnessError) as error:
        await workflow._verify(state)
    assert error.value.code == "COLLECTED_SOURCE_CHANGED"


@pytest.mark.asyncio
async def test_reply_workflow_collects_history_calendar_and_user_price_then_saves():
    """完整起草子图读相关邮件和日历、等待报价、恢复后保存草稿，正文不进State。"""
    from aegis_agent_worker.agent.workflows.mail.reply_draft_graph import MailReplyDraftWorkflow
    from langgraph.graph import StateGraph,START,END
    from aegis_agent_worker.agent.graph.state import TaskGraphState
    class ReplyTasks(Tasks):
        saved=None
        async def load_planning_reference(self,run):
            return {"target":{"resource_type":"email","id":str(UUID(int=11)),"source_version":"v1"},"instruction":"参考上次沟通及下周时间"}
        async def ensure_reply_draft_ready(self,run): pass
        async def read_mail(self,run,source):
            return {"source_version":"v1","subject":"续约","body_text":"ORIGINAL PRIVATE BODY"}
        async def start_generation_step(self,*args): return UUID(int=12)
        async def save_reply_draft(self,run,**kwargs):
            self.saved=kwargs["reply"]; return UUID(int=13)
    class Tools:
        calls=[]
        async def invoke(self,run,decision,connection):
            self.calls.append(decision.tool_name)
            return {
                "mail.messages.list":{"messages":[{"provider_message_id":"history1","subject":"上次沟通"}]},
                "mail.messages.get":{"provider_message_id":"history1","body_text":"HISTORY PRIVATE BODY"},
                "calendar.find_free_time":{"available_slots":[{"start_at":"2026-10-19T09:00:00+08:00","end_at":"2026-10-19T10:00:00+08:00"}]},
            }[decision.tool_name]
    class Agent:
        provider_name=model_name="test"
        decisions=iter([
            ReActDecision(action="call_tool",tool_name="mail.messages.list"),
            ReActDecision(action="call_tool",tool_name="mail.messages.get",arguments={"provider_message_id":"history1"}),
            ReActDecision(action="call_tool",tool_name="calendar.find_free_time",arguments={"start_at":"2026-10-19T00:00:00+08:00","end_at":"2026-10-20T00:00:00+08:00"}),
            ReActDecision(action="ask_user",question="这次报价是多少？"),
            ReActDecision(action="generate_draft"),
        ])
        async def collect_information(self,**kwargs): return next(self.decisions)
        async def summarize_evidence(self,raw): return "上次讨论续约"
        async def generate_reply_draft(self,email,**kwargs):
            assert kwargs["supplemental_context"]["user_answers"]==["报价100元"]
            assert len(kwargs["supplemental_context"]["evidence"])==3
            return {"to":["customer@example.com"],"cc":[],"subject":"续约回复","body":"供用户核对的草稿"}
    tasks,tools,inputs=ReplyTasks(),Tools(),Inputs()
    sub=MailReplyDraftWorkflow(tasks,Agent(),collection_service=tools,input_requests=inputs).build()
    root=StateGraph(TaskGraphState);root.add_node("reply",sub);root.add_edge(START,"reply");root.add_edge("reply",END)
    graph=root.compile(checkpointer=MemorySaver())
    state=dict(run_id=str(RUN.id),tenant_id=str(RUN.tenant_id),user_id=str(RUN.user_id),conversation_id="",run_type="mail_reply_draft")
    config={"configurable":{"thread_id":"full-reply"}}
    first=await graph.ainvoke(state,config)
    assert first["__interrupt__"] and tasks.saved is None
    final=await graph.ainvoke(Command(resume={"question_id":str(UUID(int=20)),"response":"报价100元"}),config)
    assert final["mail_workflow_completed"] and tasks.saved["body"]=="供用户核对的草稿"
    assert "PRIVATE BODY" not in json.dumps(final)
