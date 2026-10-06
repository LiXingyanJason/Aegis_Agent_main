"""根图的条件边适配：将路由模块的结果转换为图节点名称。"""

from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.routing.intent_router import IntentRouter
from aegis_agent_worker.agent.routing.workflow_router import WorkflowRouter


class TaskGraphRouter:
    """协调意图识别和工作流选择，不在根图中堆积业务判断。"""

    def __init__(
        self,
        intent_router: IntentRouter | None = None,
        workflow_router: WorkflowRouter | None = None,
    ) -> None:
        self._intent_router = intent_router or IntentRouter()
        self._workflow_router = workflow_router or WorkflowRouter()

    def select_workflow(self, state: TaskGraphState) -> str:
        """为已加载的会话选择下一段工作流。"""
        intent = self._intent_router.classify(state.get("latest_user_message", ""))
        return self._workflow_router.select(intent)

    def decide(self, state: TaskGraphState) -> dict[str, str]:
        """识别用户意图，创建工作流名称，供根图条件边使用。"""
        intent = self._intent_router.classify(state.get("latest_user_message", ""))
        return {"intent": intent, "workflow": self._workflow_router.select(intent)}
