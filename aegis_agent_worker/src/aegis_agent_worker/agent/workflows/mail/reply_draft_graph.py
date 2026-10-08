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


class MailReplyDraftWorkflow:
    """将读取原邮件、注入相关记忆、LLM 起草和保存草稿收敛为邮件子图。"""

    def __init__(
        self,
        mail_tasks: MailTaskService,
        mail_agent: MailAgent,
        memory_context: MemoryContextService | None = None,
    ) -> None:
        self._mail_tasks = mail_tasks
        self._mail_agent = mail_agent
        self._memory_context = memory_context

    def build(self) -> Any:
        """构建“任务预检 → 起草并保存回复”的邮件回复图。"""
        graph = StateGraph(TaskGraphState)
        graph.add_node("ensure_mail_reply_draft_ready", self._ensure_ready)
        graph.add_node("generate_mail_reply_draft", self._generate_reply_draft)
        graph.add_edge(START, "ensure_mail_reply_draft_ready")
        graph.add_edge("ensure_mail_reply_draft_ready", "generate_mail_reply_draft")
        graph.add_edge("generate_mail_reply_draft", END)
        return graph.compile()

    async def _ensure_ready(self, state: TaskGraphState) -> dict[str, str]:
        """预检任务引用；图状态不保存原邮件正文或模型草稿正文。"""
        await self._mail_tasks.ensure_reply_draft_ready(_run_from_state(state))
        return {"mail_workflow_stage": "reply_draft_ready"}

    async def _generate_reply_draft(self, state: TaskGraphState) -> dict[str, bool | str]:
        """在节点局部变量中读取邮件、注入偏好、调用专职 Agent 并保存草稿。"""
        run = _run_from_state(state)
        source = await self._mail_tasks.load_reply_source(run)
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
            instruction=source.get("instruction"),
            memory_context=memory_prompt,
        )
        await self._mail_tasks.save_reply_draft(run, source=source, reply=reply, step_id=step_id)
        return {"mail_workflow_stage": "reply_draft_completed", "mail_workflow_completed": True}


def _run_from_state(state: TaskGraphState) -> ClaimedRun:
    """从稳定图状态重建邮件回复草稿所需的任务归属对象。"""
    if state.get("run_type") != "mail_reply_draft":
        raise RuntimeError(f"邮件子图收到不匹配的任务类型：{state.get('run_type')}")
    return ClaimedRun(
        id=UUID(state["run_id"]),
        tenant_id=UUID(state["tenant_id"]),
        user_id=UUID(state["user_id"]),
        conversation_id=None,
        run_type="mail_reply_draft",
    )
