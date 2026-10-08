"""邮件 LangGraph 子图的阶段编排测试。"""

import pytest

from aegis_agent_worker.agent.workflows.mail.extraction_graph import MailExtractionWorkflow
from aegis_agent_worker.agent.workflows.mail.reply_draft_graph import MailReplyDraftWorkflow


class _FakeMailTaskService:
    """记录工作流委托的邮件领域动作，不涉及数据库、LLM 或 MCP 服务。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ensure_extraction_ready(self, run) -> None:
        """模拟摘要任务预检。"""
        assert run.run_type == "mail_extraction"
        self.calls.append("ensure_extraction_ready")

    async def ensure_reply_draft_ready(self, run) -> None:
        """模拟回复草稿任务预检。"""
        assert run.run_type == "mail_reply_draft"
        self.calls.append("ensure_reply_draft_ready")

    async def load_extraction_source(self, _run) -> dict:
        """模拟读取数据库中的摘要任务引用。"""
        self.calls.append("load_extraction_source")
        return {"connection_id": "connection", "provider_message_id": "message"}

    async def load_reply_source(self, _run) -> dict:
        """模拟读取数据库中的起草任务引用。"""
        self.calls.append("load_reply_source")
        return {"connection_id": "connection", "provider_message_id": "message"}

    async def read_mail(self, _run, _source) -> dict:
        """模拟通过 ToolGateway 读取邮件正文。"""
        self.calls.append("read_mail")
        return {"subject": "测试", "body_text": "正文"}

    async def start_generation_step(self, _run, label: str) -> str:
        """模拟建立模型运行步骤。"""
        self.calls.append(f"start:{label}")
        return "step-1"

    async def save_extraction(self, _run, **_kwargs) -> None:
        """模拟持久化摘要和待办。"""
        self.calls.append("save_extraction")

    async def save_reply_draft(self, _run, **_kwargs) -> None:
        """模拟持久化回复草稿。"""
        self.calls.append("save_reply_draft")


class _FakeMailAgent:
    """模拟邮件专职 Agent；只验证工作流在正确位置发起语义推理。"""

    @property
    def provider_name(self) -> str:
        """模拟模型供应商。"""
        return "test-provider"

    @property
    def model_name(self) -> str:
        """模拟模型名称。"""
        return "test-model"

    async def generate_extraction(self, _email) -> dict:
        """模拟模型输出的摘要结构。"""
        return {"key_points": [], "todos": [], "due_date_candidates": []}

    async def generate_reply_draft(self, _email, **_kwargs) -> dict:
        """模拟模型输出的回复草稿。"""
        return {"to": ["recipient@example.com"], "cc": [], "subject": "主题", "body": "正文"}


def _state(run_type: str) -> dict[str, str]:
    """构造邮件后台任务子图所需的最小稳定状态。"""
    return {
        "run_id": "2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6",
        "tenant_id": "2f744d3e-2a54-4e4f-aec3-bc9f0e2b6966",
        "user_id": "b0071771-d192-4c32-a4e0-7b214eec2be6",
        "run_type": run_type,
    }


@pytest.mark.asyncio
async def test_extraction_workflow_prechecks_then_generates() -> None:
    """测试邮件摘要图按“预检→生成并保存”顺序调用领域服务。"""
    service = _FakeMailTaskService()

    state = await MailExtractionWorkflow(service, _FakeMailAgent()).build().ainvoke(
        _state("mail_extraction")
    )

    assert service.calls == [
        "ensure_extraction_ready",
        "load_extraction_source",
        "read_mail",
        "start:正在生成邮件摘要",
        "save_extraction",
    ]
    assert state["mail_workflow_stage"] == "extraction_completed"
    assert state["mail_workflow_completed"] is True


@pytest.mark.asyncio
async def test_reply_draft_workflow_prechecks_then_generates() -> None:
    """测试邮件起草图按“预检→起草并保存”顺序调用领域服务。"""
    service = _FakeMailTaskService()

    state = await MailReplyDraftWorkflow(service, _FakeMailAgent()).build().ainvoke(
        _state("mail_reply_draft")
    )

    assert service.calls == [
        "ensure_reply_draft_ready",
        "load_reply_source",
        "read_mail",
        "start:正在起草回复",
        "save_reply_draft",
    ]
    assert state["mail_workflow_stage"] == "reply_draft_completed"
    assert state["mail_workflow_completed"] is True
