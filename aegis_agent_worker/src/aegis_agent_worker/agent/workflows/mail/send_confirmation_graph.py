"""邮件发送确认与恢复子图。"""

from typing import Any
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from aegis_agent_worker.agent.graph.state import TaskGraphState
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.service.mail.mail_send_service import MailSendService


class MailSendConfirmationWorkflow:
    """用 interrupt/resume 管理已保存草稿的逐项发送确认。"""

    def __init__(self, mail_send: MailSendService) -> None:
        self._mail_send = mail_send

    def build(self) -> Any:
        """构建“核验确认项 → 暂停 → 执行或拒绝”的可恢复子图。"""
        graph = StateGraph(TaskGraphState)
        graph.add_node("prepare_mail_send_confirmation", self._prepare_confirmation)
        graph.add_node("wait_for_mail_send_approval", self._wait_for_approval)
        graph.add_node("finish_mail_send", self._finish)
        graph.add_edge(START, "prepare_mail_send_confirmation")
        graph.add_edge("prepare_mail_send_confirmation", "wait_for_mail_send_approval")
        graph.add_edge("wait_for_mail_send_approval", "finish_mail_send")
        graph.add_edge("finish_mail_send", END)
        return graph.compile()

    async def _prepare_confirmation(self, state: TaskGraphState) -> dict[str, str]:
        """读取已有确认项并将数据库任务状态转为 waiting_confirmation。"""
        run = _run_from_state(state)
        return await self._mail_send.prepare_confirmation_wait(run)

    @staticmethod
    def _wait_for_approval(state: TaskGraphState) -> dict[str, str]:
        """持久化当前图状态，等待主后端审批接口以同一 run_id 恢复。"""
        decision = interrupt(
            {
                "run_id": state["run_id"],
                "approval_item_id": state["approval_item_id"],
                "status": "waiting_confirmation",
            }
        )
        return {"approval_decision": str(decision)}

    async def _finish(self, state: TaskGraphState) -> dict[str, bool]:
        """仅在恢复后执行批准或拒绝分支；批准分支仍由 Service 复核数据库决定。"""
        decision = state.get("approval_decision")
        if decision not in {"approved", "rejected"}:
            raise RuntimeError("邮件发送确认图恢复时缺少有效决定")
        await self._mail_send.execute(_run_from_state(state), decision)
        return {"mail_workflow_completed": True}


def _run_from_state(state: TaskGraphState) -> ClaimedRun:
    """从图状态重建 mail_send 任务。"""
    if state.get("run_type") != "mail_send":
        raise RuntimeError(f"邮件发送图收到不匹配的任务类型：{state.get('run_type')}")
    return ClaimedRun(
        id=UUID(state["run_id"]),
        tenant_id=UUID(state["tenant_id"]),
        user_id=UUID(state["user_id"]),
        conversation_id=None,
        run_type="mail_send",
    )
