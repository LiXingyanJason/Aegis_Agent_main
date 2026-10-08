"""邮件摘要与待办提取子图。"""

from typing import Any
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.service.mail.mail_task_service import MailTaskService


class MailExtractionWorkflow:
    """将现有邮件提取应用服务纳入可观测、可扩展的 LangGraph 子图。"""

    def __init__(self, mail_tasks: MailTaskService, mail_agent: MailAgent) -> None:
        self._mail_tasks = mail_tasks
        self._mail_agent = mail_agent

    def build(self) -> Any:
        """构建“任务预检 → 生成并保存摘要”的邮件提取图。"""
        graph = StateGraph(TaskGraphState)
        graph.add_node("ensure_mail_extraction_ready", self._ensure_ready)
        graph.add_node("generate_mail_extraction", self._generate_extraction)
        graph.add_edge(START, "ensure_mail_extraction_ready")
        graph.add_edge("ensure_mail_extraction_ready", "generate_mail_extraction")
        graph.add_edge("generate_mail_extraction", END)
        return graph.compile()

    async def _ensure_ready(self, state: TaskGraphState) -> dict[str, str]:
        """预检任务引用；图状态只保存阶段标记，不保存邮件正文。"""
        await self._mail_tasks.ensure_extraction_ready(
            _run_from_state(state, expected_type="mail_extraction")
        )
        return {"mail_workflow_stage": "extraction_ready"}

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
        return {"mail_workflow_stage": "extraction_completed", "mail_workflow_completed": True}


def _run_from_state(state: TaskGraphState, *, expected_type: str) -> ClaimedRun:
    """从稳定图状态还原后台任务的最小归属对象。"""
    if state.get("run_type") != expected_type:
        raise RuntimeError(f"邮件子图收到不匹配的任务类型：{state.get('run_type')}")
    return ClaimedRun(
        id=UUID(state["run_id"]),
        tenant_id=UUID(state["tenant_id"]),
        user_id=UUID(state["user_id"]),
        conversation_id=None,
        run_type=expected_type,
    )
