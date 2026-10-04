"""开发期 Calendar MCP JSON-RPC 服务测试。"""

from fastapi.testclient import TestClient

from app.main import app


META = {
    "aegis_connection_id": "10000000-0000-4000-8000-000000000020",
    "aegis_tenant_id": "10000000-0000-4000-8000-000000000001",
    "aegis_user_id": "10000000-0000-4000-8000-000000000010",
    "aegis_run_id": "2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6",
}


def test_tools_list_exposes_read_only_calendar_tools() -> None:
    """测试 MCP tools/list 返回两个可发现的只读日历工具。"""
    with TestClient(app) as client:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert response.status_code == 200
    names = [item["name"] for item in response.json()["result"]["tools"]]
    assert names == ["calendar.list_events", "calendar.find_free_time"]


def test_list_events_returns_mock_events() -> None:
    """测试 list_events 使用 MCP tools/call 返回固定 Mock 日程。"""
    request = {
        "jsonrpc": "2.0",
        "id": "request-1",
        "method": "tools/call",
        "params": {
            "name": "calendar.list_events",
            "arguments": {
                "start_at": "2026-10-05T00:00:00+00:00",
                "end_at": "2026-10-06T00:00:00+00:00",
            },
            "_meta": META,
        },
    }
    with TestClient(app) as client:
        response = client.post("/mcp", json=request)

    result = response.json()["result"]["structuredContent"]
    assert result["source"] == "mock_calendar"
    assert result["events"][0]["title"] == "项目同步"


def test_find_free_time_returns_working_hour_slots() -> None:
    """测试 find_free_time 会排除 Mock 忙碌事件并返回满足时长的空闲段。"""
    request = {
        "jsonrpc": "2.0",
        "id": "request-2",
        "method": "tools/call",
        "params": {
            "name": "calendar.find_free_time",
            "arguments": {
                "start_at": "2026-10-05T00:00:00+00:00",
                "end_at": "2026-10-06T00:00:00+00:00",
                "duration_minutes": 30,
                "participants": ["wangmin@example.com"],
            },
            "_meta": META,
        },
    }
    with TestClient(app) as client:
        response = client.post("/mcp", json=request)

    result = response.json()["result"]["structuredContent"]
    assert result["participant_resolution"] == "mock_current_user_only"
    assert result["available_slots"]
