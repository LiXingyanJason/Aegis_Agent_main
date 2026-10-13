"""阶段4.1：固定生产根图双入口与恢复行为，不评估真实模型分类准确率。"""
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, END, StateGraph
from langgraph.types import Command, interrupt

from aegis_agent_worker.agent.contracts.decision import ConversationCapability, MainDecision
from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.graph.task_graph import TaskGraph, TaskGraphDependencies
from aegis_agent_worker.agent.workflows.calendar.factory import CalendarWorkflowFactory
from aegis_agent_worker.agent.workflows.mail.factory import MailWorkflowFactory


class Main:
    provider_name = model_name = "baseline-mock"
    def __init__(self, decision):
        self.decision, self.calls = decision, 0
    async def decide(self, **kwargs):
        self.calls += 1
        return self.decision


class Context:
    def __init__(self):
        self.loads = 0
    async def load_history(self, *args):
        self.loads += 1
        return [{"role": "user", "content": "基线任务"}]


class DecisionContext:
    def __init__(self):
        self.audits = []
    async def load_references(self, run):
        return []
    async def record_decision(self, *args, **kwargs):
        self.audits.append(kwargs)


class LLM:
    provider_name = model_name = "baseline-mock"
    async def complete(self, messages):
        return "基线回复"


class Lifecycle:
    async def start_llm_generation(self, *args):
        return UUID(int=10)


class Factory:
    """用无外部副作用的子图替身验证实际TaskGraph和MainDecisionWorkflow编排。"""
    def __init__(self, pause=None):
        self.visits, self.pause = [], pause
    def register(self, registry):
        for name in ("calendar_read", "calendar_confirmation", "mail_query", "mail_extraction",
                     "mail_reply_draft", "mail_send_confirmation", "mail_draft_revision"):
            def node(state, name=name):
                self.visits.append(name)
                if name == "calendar_confirmation":
                    decision = interrupt({"status": "waiting_confirmation"})
                    return {"assistant_content": "已批准" if decision == "approved" else "已拒绝"}
                if name == "mail_reply_draft" and self.pause == "input":
                    value = interrupt({"question_id": str(UUID(int=20)), "status": "waiting_input"})
                    return {"mail_workflow_completed": True, "user_answers": [value["response"]]}
                if name == "calendar_read":
                    return {"tool_context": "可信日历摘要"}
                return {"mail_workflow_completed": True}
            graph = StateGraph(TaskGraphState)
            graph.add_node("execute", node)
            graph.add_edge(START, "execute")
            graph.add_edge("execute", END)
            registry.register(name, graph.compile())
        for domain, intent, workflow in (
            ("calendar", "list_events", "calendar_read"),
            ("calendar", "find_free_time", "calendar_read"),
            ("calendar", "arrange_meeting", "calendar_confirmation"),
            ("mail", "query_mail", "mail_query"),
        ):
            registry.register_capability(ConversationCapability(
                domain=domain, intent=intent, workflow=workflow, description="基线能力",
            ))


def assemble(decision, pause=None):
    main, context, audit, factory = Main(decision), Context(), DecisionContext(), Factory(pause)
    graph = TaskGraph(TaskGraphDependencies(
        context_service=context, lifecycle=Lifecycle(), llm_client=LLM(), system_prompt="基线",
        workflow_factories=(factory,),
        main_agent=main, decision_context_service=audit,
    )).build(checkpointer=MemorySaver())
    return graph, main, context, audit, factory


def initial(run_type="conversation"):
    run_id = str(uuid4())
    state = dict(run_id=run_id, tenant_id=str(UUID(int=2)), user_id=str(UUID(int=3)),
                 conversation_id=str(UUID(int=4)) if run_type == "conversation" else "",
                 run_type=run_type)
    return state, {"configurable": {"thread_id": run_id}}


@pytest.mark.asyncio
@pytest.mark.parametrize("intent,domain,workflow", [
    ("list_events", "calendar", "calendar_read"),
    ("find_free_time", "calendar", "calendar_read"),
    ("arrange_meeting", "calendar", "calendar_confirmation"),
    ("query_mail", "mail", "mail_query"),
])
async def test_conversation_uses_main_agent_and_registered_capability(intent, domain, workflow):
    """普通对话先加载上下文并调用主Agent，然后进入目录声明的领域图。"""
    graph, main, context, audit, factory = assemble(MainDecision(
        decision="delegate", domain=domain, intent=intent, instruction="基线要求"))
    state, config = initial()
    await graph.ainvoke(state, config)
    assert main.calls == context.loads == 1
    assert factory.visits == [workflow] and len(audit.audits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [
    MainDecision(decision="reply"),
    MainDecision(decision="clarify", clarification_question="请明确其中一项任务"),
])
async def test_reply_and_ambiguity_never_enter_domain_graph(decision):
    """普通回复或复合任务澄清不触发领域执行，澄清保持已校验问题。"""
    graph, main, context, _, factory = assemble(decision)
    state, config = initial()
    result = await graph.ainvoke(state, config)
    assert main.calls == context.loads == 1 and not factory.visits
    assert result["assistant_content"] == (decision.clarification_question or "基线回复")


@pytest.mark.asyncio
@pytest.mark.parametrize("run_type,workflow", [
    ("mail_extraction", "mail_extraction"), ("mail_reply_draft", "mail_reply_draft"),
    ("mail_send", "mail_send_confirmation"), ("mail_draft_revision", "mail_draft_revision"),
])
async def test_explicit_page_tasks_bypass_main_agent_and_conversation_context(run_type, workflow):
    """页面任务直达对应子图，不读取聊天、不调用主Agent、不生成主路由审计。"""
    graph, main, context, audit, factory = assemble(MainDecision(decision="reply"))
    state, config = initial(run_type)
    await graph.ainvoke(state, config)
    assert main.calls == context.loads == 0 and not audit.audits
    assert factory.visits == [workflow]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_calendar_approval_resume_keeps_thread_and_skips_reclassification(decision):
    """会议批准／拒绝恢复原检查点，不重新加载会话或调用主Agent。"""
    graph, main, context, _, _ = assemble(MainDecision(
        decision="delegate", domain="calendar", intent="arrange_meeting", instruction="会议"))
    state, config = initial()
    assert (await graph.ainvoke(state, config))["__interrupt__"]
    result = await graph.ainvoke(Command(resume=decision), config)
    assert result["run_id"] == state["run_id"] and main.calls == context.loads == 1
    assert result["assistant_content"] == ("已批准" if decision == "approved" else "已拒绝")


@pytest.mark.asyncio
async def test_reply_input_resume_never_calls_main_agent():
    """页面起草的补充问答恢复同一thread，与审批恢复值不同，始终不经过主分类。"""
    graph, main, context, _, _ = assemble(MainDecision(decision="reply"), pause="input")
    state, config = initial("mail_reply_draft")
    assert (await graph.ainvoke(state, config))["__interrupt__"]
    result = await graph.ainvoke(Command(resume={"question_id": str(UUID(int=20)),
                                                "response": "报价100元"}), config)
    assert result["run_id"] == state["run_id"] and result["user_answers"] == ["报价100元"]
    assert result["mail_workflow_completed"] and main.calls == context.loads == 0


def test_production_factories_are_the_only_conversation_capability_catalog():
    """实际工厂只开放日历3能力和邮件查询，不把页面摘要／发送／修改图开放给聊天。"""
    dependency = object()
    registry = GraphRegistry()
    CalendarWorkflowFactory(dependency, dependency, dependency).register(registry)
    MailWorkflowFactory(dependency, dependency, dependency, query_service=dependency,
                        artifact_service=dependency, revision_service=dependency).register(registry)
    assert {(c.domain, c.intent, c.workflow) for c in registry.capabilities()} == {
        ("calendar", "list_events", "calendar_read"),
        ("calendar", "find_free_time", "calendar_read"),
        ("calendar", "arrange_meeting", "calendar_confirmation"),
        ("mail", "query_mail", "mail_query"),
    }
    assert all(c.workflow in registry.names() for c in registry.capabilities())


def test_root_dependencies_no_longer_expose_legacy_router():
    """根图只接收主决策依赖，不再暴露无用的关键词router构造参数。"""
    from dataclasses import fields
    assert "router" not in {field.name for field in fields(TaskGraphDependencies)}
