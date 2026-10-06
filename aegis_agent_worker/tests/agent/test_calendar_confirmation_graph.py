"""日历 LangGraph 中断与恢复测试。"""

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from aegis_agent_worker.agent.workflows.calendar.confirmation_graph import (
    build_calendar_confirmation_graph,
)


@pytest.mark.asyncio
async def test_calendar_graph_interrupts_before_write_and_resumes_once() -> None:
    """未批准时不写入；批准后才执行一次创建事件节点。"""
    calls: list[str] = []

    async def load_context(_state): return {}
    async def query(_state): return {"availability": {"slots": ["2026-10-06T09:00:00+08:00"]}}
    async def draft(_state): return {"draft_id": "draft-1"}
    async def approval(_state): return {"approval_item_id": "approval-1"}
    async def create_event(_state):
        calls.append("create_event")
        return {"external_event_id": "mock-event-1"}

    graph = build_calendar_confirmation_graph(load_context=load_context, query_availability=query, create_draft=draft, create_approval=approval, create_event=create_event).compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "run-1"}}
    paused = await graph.ainvoke({"run_id": "run-1"}, config)
    assert paused["__interrupt__"]
    assert calls == []
    completed = await graph.ainvoke(Command(resume="approved"), config)
    assert completed["external_event_id"] == "mock-event-1"
    assert calls == ["create_event"]
