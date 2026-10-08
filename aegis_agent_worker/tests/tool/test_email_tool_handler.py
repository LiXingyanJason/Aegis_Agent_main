"""邮件 MCP 工具在 Worker 统一 Gateway 中的适配测试。"""

from types import SimpleNamespace
from uuid import UUID

import pytest

from aegis_agent_worker.tool.contracts import ToolContext, ToolDefinition, ToolInvocation, ToolResult
from aegis_agent_worker.tool.email.handler import EmailMCPToolHandler
from aegis_agent_worker.tool.gateway import ToolGateway
from aegis_agent_worker.tool.registry import ToolRegistry


class _FakeEmailClient:
    """记录 Handler 转交给 MCP Client 的受控工具请求。"""

    def __init__(self) -> None:
        self.request: tuple[str, ToolContext, dict] | None = None

    async def call(self, tool_name: str, context: ToolContext, arguments: dict) -> ToolResult:
        """返回最小结构化邮件结果。"""
        self.request = (tool_name, context, arguments)
        return ToolResult(
            response_payload={"provider_message_id": "mock-message-1"},
            output_summary={"provider_message_id": "mock-message-1"},
        )


class _FakeConnectionRepository:
    """模拟 Gateway 依据工具定义校验邮件连接与 scope。"""

    def __init__(self) -> None:
        self.arguments: dict | None = None

    async def find_active_connection(self, _session, **kwargs):
        """记录查询条件并返回属于当前用户的邮箱连接。"""
        self.arguments = kwargs
        return SimpleNamespace(id=UUID("10000000-0000-4000-8000-000000000010"), provider="gmail")


@pytest.mark.asyncio
async def test_email_handler_uses_gateway_injected_tool_name() -> None:
    """测试邮件 Handler 只使用 Gateway 注入的已注册工具名调用 MCP Client。"""
    client = _FakeEmailClient()
    handler = EmailMCPToolHandler(client)
    context = ToolContext(
        tenant_id=UUID("2f744d3e-2a54-4e4f-aec3-bc9f0e2b6966"),
        user_id=UUID("b0071771-d192-4c32-a4e0-7b214eec2be6"),
        run_id=UUID("2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6"),
        connection_id=UUID("10000000-0000-4000-8000-000000000010"),
    )

    result = await handler.invoke(
        context,
        {"__tool_name": "mail.messages.get", "provider_message_id": "mock-message-1"},
    )

    assert client.request == (
        "mail.messages.get",
        context,
        {"provider_message_id": "mock-message-1"},
    )
    assert result.response_payload["provider_message_id"] == "mock-message-1"


@pytest.mark.asyncio
async def test_gateway_resolves_mail_connection_from_tool_definition() -> None:
    """测试 Gateway 根据邮件工具声明统一校验 provider、mail.read scope 与指定连接。"""
    registry = ToolRegistry()
    handler = EmailMCPToolHandler(_FakeEmailClient())
    registry.register(
        ToolDefinition(
            name="mail.messages.get",
            version="1.0",
            risk_level="read",
            mcp_server="email-mcp",
            description="读取邮件",
            connection_providers=("gmail", "outlook_mail"),
            required_connection_scope="mail.read",
        ),
        handler,
    )
    connections = _FakeConnectionRepository()
    gateway = ToolGateway(registry, connections)
    tenant_id = UUID("2f744d3e-2a54-4e4f-aec3-bc9f0e2b6966")
    user_id = UUID("b0071771-d192-4c32-a4e0-7b214eec2be6")
    run_id = UUID("2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6")
    connection_id = UUID("10000000-0000-4000-8000-000000000010")

    prepared = await gateway.prepare(
        object(),
        tenant_id=tenant_id,
        user_id=user_id,
        run_id=run_id,
        invocation=ToolInvocation("mail.messages.get", {"provider_message_id": "mock-message-1"}),
        required_connection_id=connection_id,
    )

    assert connections.arguments == {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "providers": ("gmail", "outlook_mail"),
        "required_scope": "mail.read",
        "required_connection_id": connection_id,
    }
    assert prepared.context.connection_id == connection_id
