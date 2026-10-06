"""统一执行或恢复 LangGraph 任务图。"""

from typing import Any

from langgraph.types import Command

from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.repository.run_repository import ClaimedRun


class GraphRunner:
    """以 run_id 作为 LangGraph thread_id，保证同一任务拥有稳定执行标识。"""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    async def invoke(self, run: ClaimedRun) -> TaskGraphState:
        """从根图起点执行一个已领取的任务。"""
        return await self._graph.ainvoke(
            {
                "run_id": str(run.id),
                "tenant_id": str(run.tenant_id),
                "user_id": str(run.user_id),
                "conversation_id": str(run.conversation_id),
            },
            self._config(run.id),
        )

    async def resume(self, run_id: str, decision: str) -> TaskGraphState:
        """供后续人工审批工作流以同一 thread_id 恢复执行。"""
        return await self._graph.ainvoke(Command(resume=decision), self._config(run_id))

    @staticmethod
    def _config(run_id: object) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": str(run_id)}}
