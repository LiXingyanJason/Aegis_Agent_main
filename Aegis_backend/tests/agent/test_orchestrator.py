"""最小 Agent 编排链路测试。"""

from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from app.agent.orchestrator import AgentOrchestrator
from app.llm.client import LLMMessage
from app.repository.run_repository import ClaimedRun


class _FakeSession:
    """满足租户事务设置 RLS 变量所需的最小异步会话。"""

    @asynccontextmanager
    async def begin(self):
        """模拟数据库事务。"""
        yield self

    async def execute(self, _statement, _parameters):
        """忽略测试中的 RLS set_config 语句。"""


class _FakeDatabase:
    """每次调用均提供一个独立的模拟会话。"""

    @asynccontextmanager
    async def session(self):
        """模拟数据库会话工厂。"""
        yield _FakeSession()


class _FakeLLMClient:
    """记录模型输入并返回固定回复。"""

    def __init__(self) -> None:
        self.messages: list[LLMMessage] = []

    @property
    def provider_name(self) -> str:
        """模拟模型供应商元数据。"""
        return "openai"

    @property
    def model_name(self) -> str:
        """模拟模型名称元数据。"""
        return "test-model"

    async def complete(self, messages: list[LLMMessage]) -> str:
        """记录上下文并返回模拟 LLM 回复。"""
        self.messages = messages
        return "这是模型生成的回复。"


class _FakeRunRepository:
    """记录 Agent 编排调用的仓储替身。"""

    def __init__(self, run: ClaimedRun) -> None:
        self.run = run
        self.completed: dict[str, object] | None = None

    async def claim_queued_run(self, _session, run_id: UUID, **_kwargs) -> ClaimedRun | None:
        """模拟成功领取 queued 任务。"""
        return self.run if run_id == self.run.id else None

    async def load_conversation_history(self, _session, **_kwargs):
        """返回会话上下文。"""
        return [{"role": "user", "content": "帮我整理今天的任务"}]

    async def create_running_llm_step(self, _session, **_kwargs) -> UUID:
        """返回模拟执行步骤标识。"""
        return UUID("734977f1-0a41-4b4f-b9bd-0e960de6f9d2")

    async def complete_run_with_assistant_message(self, _session, **kwargs) -> None:
        """记录最终持久化参数。"""
        self.completed = kwargs


@pytest.mark.asyncio
async def test_orchestrator_claims_run_calls_llm_and_persists_reply() -> None:
    """测试 queued run 可完整经过领取、上下文构建、模型调用和回复保存。"""
    run = ClaimedRun(
        id=UUID("2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6"),
        tenant_id=UUID("2f744d3e-2a54-4d4f-aec3-bc9f0e2b6966"),
        user_id=UUID("b0071771-d192-4c32-a4e0-7b214eec2be6"),
        conversation_id=UUID("8bfc8088-26d5-4c19-8876-7caa364b59c0"),
    )
    repository = _FakeRunRepository(run)
    llm_client = _FakeLLMClient()
    orchestrator = AgentOrchestrator(_FakeDatabase(), llm_client, repository)

    await orchestrator.execute(run.id)

    assert llm_client.messages[0].role == "system"
    assert llm_client.messages[1] == LLMMessage(role="user", content="帮我整理今天的任务")
    assert repository.completed is not None
    assert repository.completed["assistant_content"] == "这是模型生成的回复。"
