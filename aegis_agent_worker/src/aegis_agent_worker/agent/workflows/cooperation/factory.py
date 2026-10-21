"""仅当专家和确定性审批续接均就绪时，才向根图注册团队能力。"""
from dataclasses import dataclass
from typing import Any

from langgraph.graph import START, END, StateGraph

from aegis_agent_worker.agent.contracts.decision import ConversationCapability
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.workflows.cooperation.team_workflow import CooperationTeamWorkflow


@dataclass(frozen=True)
class CooperationWorkflowFactory:
    agent: Any
    team_registry: Any
    event_service: Any
    approval_workflow: Any
    policy_provider: Any

    def register(self, registry):
        self.team_registry.require_ready()
        if not callable(getattr(self.approval_workflow, "ainvoke", None)):
            raise ValueError("团队入口必须绑定确定性审批续接，不能只生成草稿后假装run完成")
        if not self.policy_provider.current().cooperation.enabled:
            raise ValueError("团队能力与策略未同时启用")
        graph = StateGraph(TaskGraphState)
        graph.add_node("team", CooperationTeamWorkflow(self.agent, self.team_registry,
            self.event_service, self.policy_provider).build())
        graph.add_node("approval", self.approval_workflow)
        graph.add_edge(START, "team")
        graph.add_conditional_edges("team", lambda state: state.get("cooperation_status") == "draft_ready",
                                    {True: "approval", False: END})
        graph.add_edge("approval", END)
        registry.register("cooperation_meeting", graph.compile())
        registry.register_capability(ConversationCapability(domain="cooperation", intent="mail_calendar_meeting",
            workflow="cooperation_meeting", description="按续约邮件要求查询日历并生成会议草稿，最终写入必须人工确认"))
