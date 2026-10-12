"""纯 MCP SDK Streamable HTTP 集成测试。"""

from starlette.testclient import TestClient
import json
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


def _client(tmp_path) -> TestClient:
    data_path = tmp_path / "calendar.json"
    data_path.write_text(json.dumps([{
        "title": "项目同步", "start_at": "2026-10-05T07:00:00Z",
        "end_at": "2026-10-05T07:30:00Z", "attendees": [], "description": "固定测试数据",
    }]), encoding="utf-8")
    settings = Settings(provider="mock", transport="streamable-http", host="127.0.0.1", port=9001, streamable_http_path="/mcp", allowed_hosts=("127.0.0.1",))
    from dataclasses import replace
    server = create_server(replace(settings, mock_data_path=data_path))
    return TestClient(server.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)))


def _request(client: TestClient, request_id: int, method: str, params: dict) -> dict:
    response = client.post("/mcp/", headers=HEADERS, json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    assert response.status_code == 200
    return response.json()


def test_tools_list_exposes_calendar_tools(tmp_path) -> None:
    """测试 SDK 暴露列表、详情、空闲查询与审批后创建工具。"""
    with _client(tmp_path) as client:
        response = _request(client, 1, "tools/list", {})
    assert [tool["name"] for tool in response["result"]["tools"]] == ["calendar.list_events", "calendar.get_event", "calendar.find_free_time", "calendar.create_event"]


def test_list_events_returns_mock_events(tmp_path) -> None:
    """测试事件工具经过 CalendarService 返回结构化数据。"""
    with _client(tmp_path) as client:
        response = _request(client, 2, "tools/call", {"name": "calendar.list_events", "arguments": {"start_at": "2026-10-05T00:00:00+00:00", "end_at": "2026-10-06T00:00:00+00:00"}, "_meta": META})
    assert response["result"]["isError"] is False
    assert response["result"]["structuredContent"]["events"][0]["title"] == "项目同步"


def test_find_free_time_and_invalid_context(tmp_path) -> None:
    """测试可用时间工具以及无效上下文的 MCP 错误响应。"""
    with _client(tmp_path) as client:
        success = _request(client, 3, "tools/call", {"name": "calendar.find_free_time", "arguments": {"start_at": "2026-10-05T00:00:00+00:00", "end_at": "2026-10-06T00:00:00+00:00", "duration_minutes": 30}, "_meta": META})
        failure = _request(client, 4, "tools/call", {"name": "calendar.list_events", "arguments": {"start_at": "2026-10-05T00:00:00+00:00", "end_at": "2026-10-06T00:00:00+00:00"}, "_meta": {}})
    assert success["result"]["structuredContent"]["available_slots"]
    assert failure["result"]["isError"] is True


def test_page_context_stable_ids_details_and_created_event(tmp_path) -> None:
    """页面 request_id 可读取详情；旧记录 ID 补齐，创建后详情保留描述。"""
    meta = {key: value for key, value in META.items() if key != "aegis_run_id"}
    meta["aegis_request_id"] = META["aegis_run_id"]
    with _client(tmp_path) as client:
        def call(name, arguments, context=meta):
            result = _request(client, 10, "tools/call", {"name": name, "arguments": arguments, "_meta": context})
            assert not result["result"]["isError"]
            return result["result"]["structuredContent"]

        args = {"start_at": "2026-10-05T07:15:00Z", "end_at": "2026-10-05T08:00:00Z"}
        first = call("calendar.list_events", args)["events"][0]
        assert first["external_event_id"].startswith("mock-legacy-")
        assert call("calendar.list_events", args)["events"][0]["external_event_id"] == first["external_event_id"]
        detail = call("calendar.get_event", {"external_event_id": first["external_event_id"]})
        assert detail["event"]["description"] == "固定测试数据"
        assert call("calendar.get_event", {"external_event_id": "absent"})["event"] is None
        created = call("calendar.create_event", {
            "start_at": "2026-10-05T09:00:00Z", "end_at": "2026-10-05T09:50:00Z",
            "title": "总结", "description": "创建后可读取", "idempotency_key": "test-calendar-page",
        }, META)
        detail = call("calendar.get_event", {"external_event_id": created["external_event_id"]})
        assert detail["event"]["description"] == "创建后可读取"
        assert call("calendar.list_events", {
            "start_at": "2026-10-05T07:30:00Z", "end_at": "2026-10-05T09:00:00Z",
        })["events"] == []
    saved = json.loads((tmp_path / "calendar.json").read_text(encoding="utf-8"))
    assert saved[0]["external_event_id"] == first["external_event_id"]
