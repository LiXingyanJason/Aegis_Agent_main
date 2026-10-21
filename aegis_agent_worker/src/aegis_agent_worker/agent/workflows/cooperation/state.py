"""根图兼容字段与团队私有状态分开，专家通过显式映射接收输入。"""
from typing import Any

from aegis_agent_worker.agent.graph.state import TaskGraphState


class CooperationState(TaskGraphState, total=False):
    cooperation_goal: str
    cooperation_round: int
    cooperation_delegations: list[dict[str, Any]]
    cooperation_results: list[dict[str, Any]]
    cooperation_decision: dict[str, Any]
    cooperation_pending_result: dict[str, Any]
    cooperation_repeated_results: int
