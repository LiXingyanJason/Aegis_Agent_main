"""纯 MCP SDK Streamable HTTP 集成测试。"""

from starlette.testclient import TestClient
from mcp.server.transport_security import TransportSecuritySettings

from aegis_email_mcp.config import Settings
from aegis_email_mcp.server import create_server


META = {
    "aegis_connection_id": "10000000-0000-4000-8000-000000000021",
    "aegis_tenant_id": "10000000-0000-4000-8000-000000000001",
    "aegis_user_id": "10000000-0000-4000-8000-000000000010",
    "aegis_run_id": "2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6",
}
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-03-26",
    "Host": "127.0.0.1",
}


def _client() -> TestClient:
    """创建只在测试进程内运行的 Streamable HTTP MCP 客户端。"""
    settings = Settings(
        provider="mock",
        transport="streamable-http",
        host="127.0.0.1",
        port=9002,
        streamable_http_path="/mcp",
        allowed_hosts=("127.0.0.1",),
    )
    server = create_server(settings)
    return TestClient(
        server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
    )


def _request(client: TestClient, request_id: int, method: str, params: dict) -> dict:
    """发送一条 JSON-RPC MCP 请求。"""
    response = client.post(
        "/mcp/",
        headers=HEADERS,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )
    assert response.status_code == 200
    return response.json()


def test_tools_list_exposes_two_read_only_email_tools() -> None:
    """测试 MCP SDK 能发现两个只读邮件工具。"""
    with _client() as client:
        response = _request(client, 1, "tools/list", {})
    assert [tool["name"] for tool in response["result"]["tools"]] == [
        "mail.messages.list",
        "mail.messages.get",
    ]


def test_list_messages_returns_mock_mail_without_body() -> None:
    """测试列表工具返回本地快照，但不泄露邮件正文。"""
    with _client() as client:
        response = _request(client, 2, "tools/call", {"name": "mail.messages.list", "arguments": {}, "_meta": META})
    content = response["result"]["structuredContent"]
    assert response["result"]["isError"] is False
    assert content["messages"][0]["subject"] == "2026 年服务续约确认"
    assert "body_text" not in content["messages"][0]


def test_get_message_returns_body_and_rejects_invalid_context() -> None:
    """测试单封邮件正文读取，以及无效上下文的 MCP 错误响应。"""
    with _client() as client:
        success = _request(client, 3, "tools/call", {"name": "mail.messages.get", "arguments": {"provider_message_id": "mock-mail-renewal-001"}, "_meta": META})
        failure = _request(client, 4, "tools/call", {"name": "mail.messages.list", "arguments": {}, "_meta": {}})
    assert "2026-10-09" in success["result"]["structuredContent"]["body_text"]
    assert failure["result"]["isError"] is True
