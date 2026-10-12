"""邮件摘要与待办提取子图。"""

from typing import Any
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.service.mail.mail_task_service import MailTaskService
from aegis_agent_worker.agent.workflows.mail.planning_workflow import MailPlanningWorkflow, finish_mail_planning
from aegis_agent_worker.agent.contracts.mail import MailDecision


class MailExtractionWorkflow:
    """将现有邮件提取应用服务纳入可观测、可扩展的 LangGraph 子图。"""

    def __init__(self, mail_tasks: MailTaskService, mail_agent: MailAgent, planning_audit=None) -> None:
        self._mail_tasks = mail_tasks
        self._mail_agent = mail_agent
        self._planning_audit = planning_audit

    def build(self) -> Any:
        """构建“任务预检 → 生成并保存摘要”的邮件提取图。"""
        graph = StateGraph(TaskGraphState)
        graph.add_node("mail_planning", MailPlanningWorkflow(
            self._mail_agent, "extract_mail", self._mail_tasks, self._planning_audit).build())
        graph.add_node("finish_mail_planning", finish_mail_planning)
        graph.add_node("ensure_mail_extraction_ready", self._ensure_ready)
        graph.add_node("generate_mail_extraction", self._generate_extraction)
        graph.add_edge(START, "mail_planning")
        graph.add_conditional_edges("mail_planning",
            lambda state: "finish" if state.get("mail_planning_error") else "execute",
            {"finish": "finish_mail_planning", "execute": "ensure_mail_extraction_ready"})
        graph.add_edge("finish_mail_planning", END)
        graph.add_edge("ensure_mail_extraction_ready", "generate_mail_extraction")
        graph.add_edge("generate_mail_extraction", END)
        return graph.compile()

    async def _ensure_ready(self, state: TaskGraphState) -> dict[str, str]:
        """预检任务引用；图状态只保存阶段标记，不保存邮件正文。"""
        run = _run_from_state(state, expected_type="mail_extraction")
        decision = MailDecision.model_validate(state["mail_decision"])
        if decision.action != "extract":
            raise ValueError("摘要入口不允许其他动作")
        if run.run_type == "conversation":
            await self._mail_tasks.prepare_conversation_handoff(run, state["handoff"])
        await self._mail_tasks.ensure_extraction_ready(run)
        return {"mail_workflow_stage": "extraction_ready", "plan": decision.model_dump(mode="json")}

    async def _generate_extraction(self, state: TaskGraphState) -> dict[str, bool | str]:
        """在节点局部变量中读取邮件、调用专职 Agent，并由 Service 保存结果。"""
        run = _run_from_state(state, expected_type="mail_extraction")
        source = await self._mail_tasks.load_extraction_source(run)
        email = await self._mail_tasks.read_mail(run, source)
        step_id = await self._mail_tasks.start_generation_step(run, "正在生成邮件摘要")
        result = await self._mail_agent.generate_extraction(email)
        await self._mail_tasks.save_extraction(
            run,
            source=source,
            email=email,
            result=result,
            step_id=step_id,
            model_provider=self._mail_agent.provider_name,
            model_name=self._mail_agent.model_name,
        )
        if run.conversation_id is not None:
            return {
                "mail_workflow_stage": "extraction_completed", "mail_workflow_completed": False,
                "assistant_content": "邮件摘要已生成：\n" + "\n".join(result["key_points"]),
            }
        return {"mail_workflow_stage": "extraction_completed", "mail_workflow_completed": True}


def _run_from_state(state: TaskGraphState, *, expected_type: str) -> ClaimedRun:
    """从稳定图状态还原后台任务的最小归属对象。"""
    conversation = state.get("run_type") == "conversation"
    expected_intent = "extract_mail"
    if conversation and state.get("handoff", {}).get("intent") != expected_intent:
        raise RuntimeError("邮件子图收到不匹配的交接意图")
    if not conversation and state.get("run_type") != expected_type:
        raise RuntimeError(f"邮件子图收到不匹配的任务类型：{state.get('run_type')}")
    return ClaimedRun(
        id=UUID(state["run_id"]),
        tenant_id=UUID(state["tenant_id"]),
        user_id=UUID(state["user_id"]),
        conversation_id=UUID(state["conversation_id"]) if conversation else None,
        run_type="conversation" if conversation else expected_type,
    )
