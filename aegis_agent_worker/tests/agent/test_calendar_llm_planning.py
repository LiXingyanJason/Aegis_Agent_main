"""日历受控 LLM 规划、指定时段检查与真实确认子图回归。"""

import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from aegis_agent_worker.agent.contracts.calendar import CalendarDecision, CalendarMeetingDraftProposal
from aegis_agent_worker.agent.contracts.decision import AgentHandoff
from aegis_agent_worker.agent.specialists.calendar_agent import CalendarAgent
from aegis_agent_worker.agent.workflows.calendar.planning_workflow import CalendarPlanningWorkflow
from aegis_agent_worker.agent.workflows.calendar.confirmation_graph import CalendarConfirmationWorkflow
from aegis_agent_worker.service.calendar.calendar_confirmation_service import CalendarConfirmationService, explicit_interval_availability
from aegis_agent_worker.llm.client import LLMError


def payload(**overrides):
    result = dict(action="prepare_meeting", time_mode="explicit_interval", title="总结",
                  start_at="2026-10-15T03:00:00+08:00", end_at="2026-10-15T03:50:00+08:00",
                  duration_minutes=50)
    result.update(overrides)
    return result


class Client:
    provider_name = "test"
    model_name = "test"
    def __init__(self, result):
        self.result, self.calls = result, 0

    async def complete(self, messages):
        self.calls += 1
        assert "Schema" in messages[0].content
        if isinstance(self.result, Exception):
            raise self.result
        return json.dumps(self.result)


def handoff(intent="arrange_meeting"):
    return AgentHandoff(domain="calendar", intent=intent, instruction="用户日历要求")


def state(message="请在2026-10-15 03:00～03:50安排50分钟总结会议"):
    return dict(run_id=str(UUID(int=1)), tenant_id=str(UUID(int=2)), user_id=str(UUID(int=3)),
                conversation_id=str(UUID(int=4)), run_type="conversation",
                latest_user_message=message, handoff=handoff().model_dump(mode="json"))


@pytest.mark.parametrize("overrides", [
    {"action": "create_event"}, {"timezone": "invalid/zone"}, {"duration_minutes": True},
    {"duration_minutes": 10}, {"end_at": "2026-10-15T02:00:00+08:00"},
    {"end_at": "2026-10-15T04:00:00+08:00"}, {"attendees": ["王敏"]},
    {"title": " "}, {"tool_name": "calendar.create_event"},
])
def test_calendar_schema_rejects_unsafe_plans(overrides):
    """未知工具、非法字段、时区、邮箱及时长矛盾在工具执行前被拒绝。"""
    with pytest.raises(ValueError):
        CalendarDecision.model_validate(payload(**overrides))


@pytest.mark.asyncio
async def test_missing_optional_meeting_title_and_attendees_do_not_block_plan():
    """时间和时长明确即可生成草稿，未指定主题用默认标题、未指定参会人为空。"""
    client = Client(payload(title=None))
    decision = await CalendarAgent(llm_client=client).plan_calendar(
        user_content="请在2026-10-15 03:00～03:50安排一个50分钟的会议。",
        handoff=handoff(),
    )
    assert decision.action == "prepare_meeting"
    assert decision.title == "待确认会议"
    assert decision.attendees == []


@pytest.mark.asyncio
async def test_llm_plan_accepts_natural_expression_and_blocks_escalation():
    """自然语义由模型规划；领域专家不能把只读交接升级为会议创建。"""
    client = Client(payload(action="find_free_time", time_mode="search_window",
                            start_at="2026-10-13T12:00:00+08:00", end_at="2026-10-13T18:00:00+08:00"))
    agent = CalendarAgent(llm_client=client)
    decision = await agent.plan_calendar(user_content="翌日午后能腾出五十分钟吗", handoff=handoff("find_free_time"))
    assert decision.action == "find_free_time"
    assert decision.to_read_plan().tool_name == "calendar.find_free_time"
    assert client.calls == 1
    with pytest.raises(ValueError):
        await CalendarAgent(llm_client=Client(payload())).plan_calendar(
            user_content="看看日程", handoff=handoff("list_events"))


@pytest.mark.asyncio
async def test_screenshot_input_repairs_model_format_once_without_changing_time():
    """截图原句时间合法；首次非空字段误填 null 可修复一次，仍保持原时段。"""
    class RepairClient(Client):
        async def complete(self, messages):
            self.calls += 1
            result = payload(start_at="2026-10-17T15:00:00+08:00",
                             end_at="2026-10-17T15:50:00+08:00")
            if self.calls == 1:
                result["preferences"] = None
            else:
                assert "preferences" in messages[-1].content
            return json.dumps(result)
    client = RepairClient(None)
    decision = await CalendarAgent(llm_client=client).plan_calendar(
        user_content="请在2026-10-17 15:00～15:50安排一个50分钟的会议。", handoff=handoff())
    assert decision.start_at.isoformat() == "2026-10-17T15:00:00+08:00"
    assert decision.end_at.isoformat() == "2026-10-17T15:50:00+08:00"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_failed_repair_is_bounded_and_logs_only_field_diagnostics(caplog):
    """修复仍失败时不执行工具；日志仅含字段类型，不暴露邮箱或原始响应。"""
    from aegis_agent_worker.agent.specialists.calendar_agent import CalendarPlanValidationError
    client = Client(payload(attendees=["private-secret@example.com"], preferences=None))
    with pytest.raises(CalendarPlanValidationError) as error:
        await CalendarAgent(llm_client=client).plan_calendar(user_content="安排会议", handoff=handoff())
    assert client.calls == 2
    assert any("preferences" in issue for issue in error.value.issues)
    assert "private-secret" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("content,start,end", [
    ("2026-10-15 03:00～03:50安排50分钟会议", "2026-10-15T03:00:00", "2026-10-15T03:50:00"),
    ("2026-10-15T03:00:00Z～2026-10-15T03:50:00Z安排50分钟会议",
     "2026-10-15T11:00:00+08:00", "2026-10-15T11:50:00+08:00"),
])
async def test_llm_plan_default_timezone_and_explicit_offset(content, start, end):
    """无偏移默认北京时间，显式 UTC 的同一瞬间不被二次改写。"""
    decision = await CalendarAgent(llm_client=Client(payload(start_at=start, end_at=end))).plan_calendar(
        user_content=content, handoff=handoff())
    assert decision.start_at.utcoffset().total_seconds() == 8 * 3600


@pytest.mark.asyncio
async def test_explicit_constraints_and_invalid_model_output_clarify():
    """模型改动原文明确时间或返回非法结构时，图只返回提示而不执行工具。"""
    client = Client(payload(start_at="2026-10-15T11:00:00+08:00", end_at="2026-10-15T11:50:00+08:00"))
    result = await CalendarPlanningWorkflow(CalendarAgent(llm_client=client)).build().ainvoke(state())
    assert result["calendar_planning_error"]
    with pytest.raises(LLMError):
        await CalendarPlanningWorkflow(CalendarAgent()).build().ainvoke(state())


def summary(decision, events):
    return {"query_range": {"start_at": decision.start_at.isoformat(), "end_at": decision.end_at.isoformat()},
            "events": events}


def test_explicit_interval_conflicts_and_half_open_boundaries():
    """凌晨可生成可信候选；冲突排除；紧邻开始和结束的事件不冲突。"""
    decision = CalendarDecision.model_validate(payload())
    empty = explicit_interval_availability(decision, summary(decision, []))
    assert len(empty["available_slots"]) == 1 and empty["scheduling_warning"]
    conflict = {"start_at": "2026-10-15T03:20:00+08:00", "end_at": "2026-10-15T04:00:00+08:00"}
    assert not explicit_interval_availability(decision, summary(decision, [conflict]))["available_slots"]
    neighbors = [
        {"start_at": "2026-10-15T02:00:00+08:00", "end_at": "2026-10-15T03:00:00+08:00"},
        {"start_at": "2026-10-15T03:50:00+08:00", "end_at": "2026-10-15T04:00:00+08:00"},
    ]
    assert explicit_interval_availability(decision, summary(decision, neighbors))["available_slots"]
    truncated = summary(decision, [])
    truncated["events_total"] = 1
    with pytest.raises(ValueError):
        explicit_interval_availability(decision, truncated)


class Execution:
    def __init__(self, conflict=False):
        self.calls, self.conflict = [], conflict

    async def execute_read_tool(self, run, context, invocation):
        self.calls.append(invocation)
        events = [{"start_at": invocation.arguments["start_at"], "end_at": invocation.arguments["end_at"]}] if self.conflict else []
        return SimpleNamespace(status="succeeded", tool_context="日历查询结果", output_summary={
            "query_range": invocation.arguments, "events": events,
        })


class Confirmation(CalendarConfirmationService):
    def __init__(self, execution):
        super().__init__(None, None, execution)
        self.created = []
        self.writes = 0

    async def create_recommended_draft(self, run, **kwargs):
        self.created.append(kwargs)
        return {"awaiting_confirmation": True, "draft_id": str(UUID(int=5)),
                "approval_item_id": str(UUID(int=6))}

    async def create_approved_event(self, run, **kwargs):
        self.writes += 1
        return "event-created"


class DraftAgent(CalendarAgent):
    async def generate_meeting_draft(self, **kwargs):
        return CalendarMeetingDraftProposal(title="总结", selected_slot_id=kwargs["candidates"][0].slot_id,
                                             reason="用户指定时间", attendees=[])


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_actual_confirmation_graph_queries_explicit_range_and_resumes(conflict):
    """真实确认子图使用日程查询核验凌晨，无冲突先暂停、批准后写入且不重做规划。"""
    client = Client(payload())
    execution = Execution(conflict)
    service = Confirmation(execution)
    graph = CalendarConfirmationWorkflow(DraftAgent(llm_client=client), service).build()
    # 外层编译 Checkpointer，和正式根图的子图恢复方式一致。
    from langgraph.graph import StateGraph, START, END
    from aegis_agent_worker.agent.graph.state import TaskGraphState
    root = StateGraph(TaskGraphState)
    root.add_node("calendar", graph)
    root.add_edge(START, "calendar")
    root.add_edge("calendar", END)
    graph = root.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "calendar-test"}}
    first = await graph.ainvoke(state(), config)
    assert execution.calls[0].tool_name == "calendar.list_events"
    assert service.writes == 0
    if conflict:
        assert "冲突" in first["assistant_content"]
        assert not service.created and not first.get("__interrupt__")
    else:
        assert first["__interrupt__"]
        assert service.created[0]["scheduling_warning"]
        result = await graph.ainvoke(Command(resume="approved"), config)
        assert "会议已创建" in result["assistant_content"]
        assert service.writes == 1 and client.calls == 1
