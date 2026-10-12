"""统一邮件规划子图：对话使用 LLM，页面指定任务直接构造受限计划。"""

from time import monotonic
from uuid import UUID

from langgraph.graph import START, END, StateGraph
from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.contracts.decision import AgentHandoff
from aegis_agent_worker.agent.contracts.mail import MailDecision
from aegis_agent_worker.agent.specialists.mail_agent import MailAgentError
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.observability.telemetry import get_tracer


class MailPlanningWorkflow:
    def __init__(self, mail_agent, expected_intent, task_service=None, audit_service=None):
        self._agent, self._intent = mail_agent, expected_intent
        self._tasks, self._audit_service = task_service, audit_service

    def build(self):
        graph = StateGraph(TaskGraphState)
        graph.add_node("infer_mail_decision", self._infer)
        graph.add_edge(START, "infer_mail_decision")
        graph.add_edge("infer_mail_decision", END)
        return graph.compile()

    async def _infer(self, state):
        started = monotonic()
        run = mail_run(state)
        source = "llm" if run.run_type == "conversation" else "explicit_task"
        outcome, decision = "validated", None
        with get_tracer(__name__).start_as_current_span("agent.mail.plan") as span:
            span.set_attribute("aegis.run_id", str(run.id))
            span.set_attribute("aegis.planning_source", source)
            try:
                if source == "llm":
                    if self._intent != "query_mail":
                        decision = MailDecision(action="clarify", clarification_question="请先定位邮件卡片，再点击卡片中的生成摘要或起草回复按钮。")
                        await self._audit(run, decision, "card_action_required", source, started)
                        return {"mail_decision": decision.model_dump(mode="json"),
                                "mail_planning_error": decision.clarification_question}
                    handoff = AgentHandoff.model_validate(state["handoff"])
                    if handoff.domain != "mail" or handoff.intent != self._intent:
                        raise ValueError("邮件交接范围无效")
                    decision = await self._agent.plan_mail(state.get("latest_user_message", ""), handoff)
                else:
                    expected = {"mail_extraction": ("extract_mail", "extract"),
                                "mail_reply_draft": ("draft_mail_reply", "draft_reply")}.get(run.run_type)
                    if not expected or expected[0] != self._intent or self._tasks is None:
                        raise ValueError("页面任务不匹配领域入口")
                    reference = await self._tasks.load_planning_reference(run)
                    decision = MailDecision(action=expected[1], **reference)
                error = decision.clarification_question if decision.action == "clarify" else ""
            except (KeyError, ValueError, MailAgentError):
                outcome = "invalid_plan"
                error = "邮件规划未通过校验，请明确查询条件或要处理的邮件。"
            except Exception:
                await self._audit(run, None, "model_failed", source, started)
                raise
            await self._audit(run, decision, outcome, source, started)
        return {"mail_decision": decision.model_dump(mode="json") if decision else {},
                "mail_planning_error": error or ""}

    async def _audit(self, run, decision, outcome, source, started):
        if self._audit_service:
            await self._audit_service.record_plan(
                run, decision=decision, outcome=outcome, planning_source=source,
                duration_ms=round((monotonic() - started) * 1000),
                model_provider=self._agent.provider_name if source == "llm" else None,
                model_name=self._agent.model_name if source == "llm" else None,
            )


def mail_run(state):
    """还原可信运行归属，页面任务没有 conversation_id。"""
    return ClaimedRun(id=UUID(state["run_id"]), tenant_id=UUID(state["tenant_id"]),
                      user_id=UUID(state["user_id"]),
                      conversation_id=UUID(state["conversation_id"]) if state.get("conversation_id") else None,
                      run_type=state.get("run_type", "conversation"))


def finish_mail_planning(state):
    """对话澄清可正常回复；后台任务参数无效须标记失败，不能冒充完成。"""
    error = state.get("mail_planning_error") or "邮件计划无效"
    if state.get("run_type", "conversation") != "conversation":
        raise MailAgentError(error)
    return {"assistant_content": error, "mail_workflow_completed": False}
