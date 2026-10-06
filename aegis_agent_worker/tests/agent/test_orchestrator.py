"""最小 Agent 编排链路测试。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest

from aegis_agent_worker.agent.orchestrator import AgentOrchestrator
from aegis_agent_worker.llm.client import LLMError, LLMMessage
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.tool.contracts import ToolContext, ToolResult


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

    def __init__(self, error: LLMError | None = None) -> None:
        self.messages: list[LLMMessage] = []
        self._error = error

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
        if self._error is not None:
            raise self._error
        return "这是模型生成的回复。"


class _FakeRunRepository:
    """记录 Agent 编排调用的仓储替身。"""

    def __init__(self, run: ClaimedRun) -> None:
        self.run = run
        self.completed: dict[str, object] | None = None
        self.failed: dict[str, object] | None = None

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

    async def create_running_tool_step(self, _session, **_kwargs) -> UUID:
        """返回模拟日历工具步骤标识。"""
        return UUID("d99ce16b-7970-46b7-8d18-6b953fdfb5fa")

    async def finish_step(self, _session, **_kwargs) -> None:
        """模拟结束日历工具步骤。"""

    async def fail_run(self, _session, **kwargs) -> None:
        """记录任务失败时传入的 LLM 步骤标识。"""
        self.failed = kwargs


class _FakeToolGateway:
    """模拟已注册、已连接的 Calendar MCP 工具网关。"""

    def __init__(self, run: ClaimedRun) -> None:
        self._run = run
        self.invoked_tool_name: str | None = None

    def get_definition(self, tool_name: str):
        """返回工具审计记录需要的只读定义。"""
        return SimpleNamespace(
            name=tool_name,
            version="1.0",
            risk_level="read",
            mcp_server="calendar-mcp",
        )

    async def prepare(self, _session, **kwargs):
        """模拟网关已完成连接和风险校验。"""
        return SimpleNamespace(
            context=ToolContext(
                tenant_id=kwargs["tenant_id"],
                user_id=kwargs["user_id"],
                run_id=kwargs["run_id"],
                connection_id=UUID("4efcc484-37db-49a6-999c-b384314ed6e2"),
            )
        )

    async def invoke(self, prepared) -> ToolResult:
        """模拟 Calendar MCP 返回一条日历事件。"""
        self.invoked_tool_name = "calendar.list_events"
        assert prepared.context.run_id == self._run.id
        return ToolResult(
            response_payload={"events": [{"title": "项目同步"}]},
            output_summary={"events": [{"title": "项目同步"}]},
        )


class _FakeToolCallRepository:
    """模拟工具调用审计表写入。"""

    async def create_running(self, _session, **_kwargs) -> UUID:
        """返回模拟工具调用 ID。"""
        return UUID("eb93150c-c1fb-4c57-957b-99791bcc5b2a")

    async def succeed(self, _session, **_kwargs) -> None:
        """模拟成功保存工具响应。"""

    async def fail(self, _session, **_kwargs) -> None:
        """模拟失败保存工具响应。"""


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


@pytest.mark.asyncio
async def test_orchestrator_routes_calendar_request_calls_tool_and_injects_result() -> None:
    """测试日程意图会调用受控日历工具，并将可信结果放入 LLM 上下文。"""
    run = ClaimedRun(
        id=UUID("2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6"),
        tenant_id=UUID("2f744d3e-2a54-4e4f-aec3-bc9f0e2b6966"),
        user_id=UUID("b0071771-d192-4c32-a4e0-7b214eec2be6"),
        conversation_id=UUID("8bfc8088-26d5-4c19-8876-7caa364b59c0"),
    )
    repository = _FakeRunRepository(run)
    async def calendar_history(_session, **_kwargs):
        return [{"role": "user", "content": "查看我明天的日程"}]
    repository.load_conversation_history = calendar_history
    llm_client = _FakeLLMClient()
    gateway = _FakeToolGateway(run)
    orchestrator = AgentOrchestrator(
        _FakeDatabase(),
        llm_client,
        repository,
        tools=gateway,
        tool_calls=_FakeToolCallRepository(),
    )

    await orchestrator.execute(run.id)

    assert gateway.invoked_tool_name == "calendar.list_events"
    assert any("Calendar MCP 工具返回" in message.content for message in llm_client.messages)


@pytest.mark.asyncio
async def test_orchestrator_marks_graph_llm_step_failed_when_model_fails() -> None:
    """测试根图内模型节点失败时，已创建的 LLM 步骤会被任务生命周期服务收尾。"""
    run = ClaimedRun(
        id=UUID("2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6"),
        tenant_id=UUID("2f744d3e-2a54-4e4f-aec3-bc9f0e2b6966"),
        user_id=UUID("b0071771-d192-4c32-a4e0-7b214eec2be6"),
        conversation_id=UUID("8bfc8088-26d5-4c19-8876-7caa364b59c0"),
    )
    repository = _FakeRunRepository(run)
    orchestrator = AgentOrchestrator(
        _FakeDatabase(),
        _FakeLLMClient(LLMError("模型服务调用失败")),
        repository,
    )

    result = await orchestrator.execute(run.id)

    assert result.status == "failed"
    assert repository.failed is not None
    assert repository.failed["step_id"] == UUID("734977f1-0a41-4b4f-b9bd-0e960de6f9d2")
