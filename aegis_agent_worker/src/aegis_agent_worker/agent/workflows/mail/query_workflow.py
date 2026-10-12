"""主对话邮件查询子图：LLM 计划、确定性查询、候选 Artifact 与回复。"""

from uuid import UUID
from langgraph.graph import StateGraph, START, END

from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.contracts.mail import MailQueryPlan, MailDecision, build_mail_candidates
from aegis_agent_worker.agent.contracts.common import ToolResultSummary
from aegis_agent_worker.agent.specialists.mail_agent import MailAgentError
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.tool.contracts import ToolError
from aegis_agent_worker.agent.workflows.mail.planning_workflow import MailPlanningWorkflow


class MailQueryWorkflow:
    def __init__(self, mail_agent, query_service, artifact_service, planning_audit=None):
        self._agent, self._query_service, self._artifacts = mail_agent, query_service, artifact_service
        self._planning_audit = planning_audit

    def build(self):
        graph = StateGraph(TaskGraphState)
        graph.add_node("mail_planning", MailPlanningWorkflow(self._agent, "query_mail",
                                                            audit_service=self._planning_audit).build())
        graph.add_node("plan_mail_query", self._plan)
        graph.add_node("execute_mail_query", self._query)
        graph.add_edge(START, "mail_planning")
        graph.add_edge("mail_planning", "plan_mail_query")
        graph.add_conditional_edges("plan_mail_query", lambda state: "finish" if state.get("mail_query_error") else "query",
                                    {"finish": END, "query": "execute_mail_query"})
        graph.add_edge("execute_mail_query", END)
        return graph.compile()

    async def _plan(self, state):
        if state.get("mail_planning_error"):
            return {"mail_query_error": state["mail_planning_error"],
                    "assistant_content": state["mail_planning_error"]}
        try:
            decision = MailDecision.model_validate(state["mail_decision"])
            if decision.action != "query":
                raise ValueError("查询入口不允许其他动作")
            plan = decision.query
        except (KeyError, ValueError, MailAgentError):
            question = "邮件查询条件尚无法形成有效计划，请明确关键词、时间范围或发件人。"
            return {"mail_query_error": question, "assistant_content": question}
        if plan.action == "clarify":
            return {"mail_query_error": plan.clarification_question, "assistant_content": plan.clarification_question}
        return {"plan": plan.model_dump(mode="json"), "mail_query_error": ""}

    async def _query(self, state):
        run = ClaimedRun(id=UUID(state["run_id"]), tenant_id=UUID(state["tenant_id"]),
                         user_id=UUID(state["user_id"]), conversation_id=UUID(state["conversation_id"]))
        plan = MailQueryPlan.model_validate(state["plan"])
        decision = MailDecision.model_validate(state["mail_decision"])
        try:
            rows, source = await self._query_service.query(run, plan, target=decision.target)
        except ToolError as error:
            return {
                "mail_query_error": error.message, "assistant_content": f"邮件查询未完成：{error.message}。",
                "tool_result": ToolResultSummary(status="failed", tool_name="mail.messages.list",
                                                summary={"error_code": error.code}).model_dump(mode="json"),
            }
        focused = decision.target is not None or bool(plan.subject_equals)
        artifact = build_mail_candidates(run, plan, rows, len(rows), source, focused=focused)
        await self._artifacts.persist(run, [artifact])
        count = artifact.payload["returned_count"]
        message = f"找到 {len(rows)} 封匹配邮件，已展示 {count} 封候选。请点击卡片按钮查看邮件、生成摘要或起草回复。"
        if len(rows) == 1:
            message = "已定位到对应邮件，请点击卡片中的按钮生成摘要、加入待办或起草回复。"
        if not rows:
            message = "没有找到符合这些查询条件的邮件。"
        if artifact.payload["truncated"]:
            message += "候选已按最近接收时间排序；可缩小查询范围查看其他邮件。"
        return {
            "artifacts": [artifact.model_dump(mode="json")],
            "tool_result": ToolResultSummary(status="succeeded", tool_name="mail.messages.list",
                                            summary=artifact.payload).model_dump(mode="json"),
            "assistant_content": message,
        }
