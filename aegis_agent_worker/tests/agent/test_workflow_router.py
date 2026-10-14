"""统一解析拒绝非法入口；替身子图访问数即业务执行次数。"""
import pytest

from aegis_agent_worker.agent.contracts.decision import ConversationCapability, MainDecision
from aegis_agent_worker.agent.routing.workflow_router import WorkflowRouter, WorkflowRoutingError
from aegis_agent_worker.agent.workflows.main.decision_workflow import MainDecisionWorkflow
from test_routing_baseline import assemble, initial, DecisionContext, Main


def capability(workflow="calendar_read"):
    return ConversationCapability(domain="calendar", intent="list_events", workflow=workflow,
                                  description="测试能力")


def decision():
    return MainDecision(decision="delegate", domain="calendar", intent="list_events", instruction="查看日历")


@pytest.mark.parametrize("caps,nodes,candidates,code", [
    ([], {"calendar_read"}, ["calendar.list_events"], "unregistered_capability"),
    ([capability()], {"calendar_read"}, [], "outside_candidate_set"),
    ([capability("unknown")], {"calendar_read"}, ["calendar.list_events"], "unregistered_workflow"),
])
def test_capability_resolution_is_fail_closed(caps, nodes, candidates, code):
    with pytest.raises(WorkflowRoutingError) as error:
        WorkflowRouter(caps, nodes).resolve_decision(decision(), candidates)
    assert error.value.code == code


@pytest.mark.parametrize("workflow", [None, "general_reply", "mail_send_confirmation", "unknown"])
def test_root_rejects_missing_or_forged_workflow(workflow):
    router = WorkflowRouter([capability()], {"calendar_read", "mail_send_confirmation"})
    with pytest.raises(WorkflowRoutingError, match="workflow_state_mismatch"):
        router.verify_conversation_route({"workflow": workflow, "main_decision": decision().model_dump()})


def test_explicit_reply_and_clarify_are_not_fallbacks():
    router = WorkflowRouter([], [])
    for value, expected in [(MainDecision(decision="reply"), "general_reply"),
                            (MainDecision(decision="clarify", clarification_question="请明确"), "clarify")]:
        assert router.verify_conversation_route({"workflow": expected,
                                                "main_decision": value.model_dump()}) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("run_type", [None, "mail_unknown", ""])
async def test_invalid_run_type_never_loads_or_executes(run_type):
    graph, main, context, audit, factory = assemble(MainDecision(decision="reply"))
    state, config = initial()
    if run_type is None:
        state.pop("run_type")
    else:
        state["run_type"] = run_type
    with pytest.raises(WorkflowRoutingError, match="invalid_run_type"):
        await graph.ainvoke(state, config)
    assert main.calls == context.loads == 0
    assert not factory.visits and not audit.audits


def test_page_task_requires_registered_workflow():
    with pytest.raises(WorkflowRoutingError, match="unregistered_workflow"):
        WorkflowRouter([], []).resolve_run_type("mail_send")


@pytest.mark.asyncio
async def test_missing_registered_node_clarifies_before_domain_execution():
    main, audit = Main(decision()), DecisionContext()
    workflow = MainDecisionWorkflow(main, audit, [capability()],
                                   workflow_router=WorkflowRouter([capability()], []))
    state, _ = initial()
    result = await workflow.build().ainvoke({**state, "latest_user_message": "查看日历"})
    assert result["workflow"] == "clarify" and not result["handoff"]
    assert audit.audits[0]["outcome"] == "unregistered_workflow"
