"""纯 MCP SDK Streamable HTTP 集成测试。"""

from starlette.testclient import TestClient
from mcp.server.transport_security import TransportSecuritySettings

from aegis_calendar_mcp.config import Settings
from aegis_calendar_mcp.server import create_server


META = {
    "aegis_connection_id": "10000000-0000-4000-8000-000000000020",
    "aegis_tenant_id": "10000000-0000-4000-8000-000000000001",
    "aegis_user_id": "10000000-0000-4000-8000-000000000010",
    "aegis_run_id": "2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6",
}
HEADERS = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-03-26", "Host": "127.0.0.1"}


def _client() -> TestClient:
    settings = Settings(provider="mock", transport="streamable-http", host="127.0.0.1", port=9001, streamable_http_path="/mcp", allowed_hosts=("127.0.0.1",))
    server = create_server(settings)
    return TestClient(server.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)))


def _request(client: TestClient, request_id: int, method: str, params: dict) -> dict:
    response = client.post("/mcp/", headers=HEADERS, json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    assert response.status_code == 200
    return response.json()


def test_tools_list_exposes_two_read_only_tools() -> None:
    """测试 MCP SDK 直接发现两个日历工具。"""
    with _client() as client:
        response = _request(client, 1, "tools/list", {})
    assert [tool["name"] for tool in response["result"]["tools"]] == ["calendar.list_events", "calendar.find_free_time", "calendar.create_event"]


def test_list_events_returns_mock_events() -> None:
    """测试事件工具经过 CalendarService 返回结构化数据。"""
    with _client() as client:
        response = _request(client, 2, "tools/call", {"name": "calendar.list_events", "arguments": {"start_at": "2026-10-05T00:00:00+00:00", "end_at": "2026-10-06T00:00:00+00:00"}, "_meta": META})
    assert response["result"]["isError"] is False
    assert response["result"]["structuredContent"]["events"][0]["title"] == "项目同步"


def test_find_free_time_and_invalid_context() -> None:
    """测试可用时间工具以及无效上下文的 MCP 错误响应。"""
    with _client() as client:
        success = _request(client, 3, "tools/call", {"name": "calendar.find_free_time", "arguments": {"start_at": "2026-10-05T00:00:00+00:00", "end_at": "2026-10-06T00:00:00+00:00", "duration_minutes": 30}, "_meta": META})
        failure = _request(client, 4, "tools/call", {"name": "calendar.list_events", "arguments": {"start_at": "2026-10-05T00:00:00+00:00", "end_at": "2026-10-06T00:00:00+00:00"}, "_meta": {}})
    assert success["result"]["structuredContent"]["available_slots"]
    assert failure["result"]["isError"] is True
