"""纯代码工作流解析；不调用模型、工具或数据库，也不授予写入权限。"""

from aegis_agent_worker.agent.routing.capability_selector import capability_id


class WorkflowRoutingError(ValueError):
    """可审计的路由拒绝；模型选择错误应澄清，内部状态错误应失败。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class WorkflowRouter:
    """用可信能力目录和根图实际节点解析两类入口。"""

    RUN_WORKFLOWS = {
        "conversation": "conversation",
        "mail_extraction": "mail_extraction",
        "mail_reply_draft": "mail_reply_draft",
        "mail_send": "mail_send_confirmation",
        "mail_draft_revision": "mail_draft_revision",
    }

    def __init__(self, capabilities, registered_workflows):
        self._capabilities = {(item.domain, item.intent): item for item in capabilities}
        self._registered = frozenset(registered_workflows)

    def resolve_capability(self, decision, candidates):
        """三重校验"""
        capability = self._capabilities.get((decision.domain, decision.intent))
        if capability is None:
            raise WorkflowRoutingError("unregistered_capability")
        if capability_id(capability) not in candidates:
            raise WorkflowRoutingError("outside_candidate_set")
        if capability.workflow not in self._registered:
            raise WorkflowRoutingError("unregistered_workflow")
        return capability

    def resolve_decision(self, decision, candidates):
        """映射决策和分支节点分支名"""
        if decision.decision == "reply":
            return "general_reply"
        if decision.decision == "clarify":
            return "clarify"
        return self.resolve_capability(decision, candidates).workflow

    def resolve_run_type(self, run_type):
        if not isinstance(run_type, str) or run_type not in self.RUN_WORKFLOWS:
            raise WorkflowRoutingError("invalid_run_type")
        workflow = self.RUN_WORKFLOWS[run_type]
        if workflow != "conversation" and workflow not in self._registered:
            raise WorkflowRoutingError("unregistered_workflow")
        return workflow

    def run_routes(self):
        """根图条件边与解析使用同一目录，只暴露实际注册的页面任务。"""
        return {"conversation": "load_context", **{
            workflow: workflow for run_type, workflow in self.RUN_WORKFLOWS.items()
            if run_type != "conversation" and workflow in self._registered
        }}

    def verify_conversation_route(self, state):
        from aegis_agent_worker.agent.contracts.decision import MainDecision

        decision = MainDecision.model_validate(state.get("main_decision"))
        expected = self.resolve_decision(decision, tuple(
            capability_id(item) for item in self._capabilities.values()))
        if state.get("workflow") != expected:
            raise WorkflowRoutingError("workflow_state_mismatch")
        return expected
