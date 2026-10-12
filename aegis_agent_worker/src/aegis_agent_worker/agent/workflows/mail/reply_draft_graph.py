"""邮件回复草稿子图。"""

from typing import Any
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
from aegis_agent_worker.config.database import TenantContext
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.service.mail.mail_task_service import MailTaskService
from aegis_agent_worker.service.memory.memory_context_service import MemoryContextService
from aegis_agent_worker.agent.workflows.mail.planning_workflow import MailPlanningWorkflow, finish_mail_planning
from aegis_agent_worker.agent.contracts.mail import MailDecision


class MailReplyDraftWorkflow:
    """将读取原邮件、注入相关记忆、LLM 起草和保存草稿收敛为邮件子图。"""

    def __init__(
        self,
        mail_tasks: MailTaskService,
        mail_agent: MailAgent,
        memory_context: MemoryContextService | None = None,
        planning_audit=None,
    ) -> None:
        self._mail_tasks = mail_tasks
        self._mail_agent = mail_agent
        self._memory_context = memory_context
        self._planning_audit = planning_audit

    def build(self) -> Any:
        """构建“任务预检 → 起草并保存回复”的邮件回复图。"""
        graph = StateGraph(TaskGraphState)
        graph.add_node("mail_planning", MailPlanningWorkflow(
            self._mail_agent, "draft_mail_reply", self._mail_tasks, self._planning_audit).build())
        graph.add_node("finish_mail_planning", finish_mail_planning)
        graph.add_node("ensure_mail_reply_draft_ready", self._ensure_ready)
        graph.add_node("generate_mail_reply_draft", self._generate_reply_draft)
        graph.add_edge(START, "mail_planning")
        graph.add_conditional_edges("mail_planning",
            lambda state: "finish" if state.get("mail_planning_error") else "execute",
            {"finish": "finish_mail_planning", "execute": "ensure_mail_reply_draft_ready"})
        graph.add_edge("finish_mail_planning", END)
        graph.add_edge("ensure_mail_reply_draft_ready", "generate_mail_reply_draft")
        graph.add_edge("generate_mail_reply_draft", END)
        return graph.compile()

    async def _ensure_ready(self, state: TaskGraphState) -> dict[str, str]:
        """预检任务引用；图状态不保存原邮件正文或模型草稿正文。"""
        run = _run_from_state(state)
        decision = MailDecision.model_validate(state["mail_decision"])
        if decision.action != "draft_reply":
            raise ValueError("起草入口不允许其他动作")
        if run.run_type == "conversation":
            await self._mail_tasks.prepare_conversation_handoff(run, state["handoff"])
        await self._mail_tasks.ensure_reply_draft_ready(run)
        return {"mail_workflow_stage": "reply_draft_ready", "plan": decision.model_dump(mode="json")}

    async def _generate_reply_draft(self, state: TaskGraphState) -> dict[str, bool | str]:
        """在节点局部变量中读取邮件、注入偏好、调用专职 Agent 并保存草稿。"""
        run = _run_from_state(state)
        source = await self._mail_tasks.load_reply_source(run)
        decision = MailDecision.model_validate(state["mail_decision"])
        instruction = source.get("instruction") or ""
        if decision.instruction and decision.instruction != instruction:
            instruction += "\n邮件专家规划要求：" + decision.instruction
        email = await self._mail_tasks.read_mail(run, source)
        memory_prompt = None
        if self._memory_context is not None:
            selected = await self._memory_context.select_for_run(
                run_id=run.id,
                context=TenantContext(tenant_id=run.tenant_id, user_id=run.user_id),
                task_text=f"邮件回复 {source.get('instruction') or ''}",
            )
            memory_prompt = self._memory_context.format_system_context(selected)
        step_id = await self._mail_tasks.start_generation_step(run, "正在起草回复")
        reply = await self._mail_agent.generate_reply_draft(
            email,
            key_points=source.get("key_points"),
            instruction=instruction,
            memory_context=memory_prompt,
        )
        draft_id = await self._mail_tasks.save_reply_draft(run, source=source, reply=reply, step_id=step_id)
        if run.conversation_id is not None:
            return {
                "mail_workflow_stage": "reply_draft_completed", "mail_workflow_completed": False,
                "assistant_content": f"已保存回复草稿「{reply['subject']}」。请在邮件管理中查看并核对，草稿尚未发送。",
                "draft_id": str(draft_id),
            }
        return {"mail_workflow_stage": "reply_draft_completed", "mail_workflow_completed": True}


def _run_from_state(state: TaskGraphState) -> ClaimedRun:
    """从稳定图状态重建邮件回复草稿所需的任务归属对象。"""
    conversation = state.get("run_type") == "conversation"
    if conversation and state.get("handoff", {}).get("intent") != "draft_mail_reply":
        raise RuntimeError("邮件子图收到不匹配的交接意图")
    if not conversation and state.get("run_type") != "mail_reply_draft":
        raise RuntimeError(f"邮件子图收到不匹配的任务类型：{state.get('run_type')}")
    return ClaimedRun(
        id=UUID(state["run_id"]),
        tenant_id=UUID(state["tenant_id"]),
        user_id=UUID(state["user_id"]),
        conversation_id=UUID(state["conversation_id"]) if conversation else None,
        run_type="conversation" if conversation else "mail_reply_draft",
    )
