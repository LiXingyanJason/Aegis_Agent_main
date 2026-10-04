"""日历 MCP 工具契约、路由和 HTTP 客户端测试。"""

import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app.agent.tool_router import AgentToolRouter
from app.tool.calendar_mcp_client import CalendarMCPClient
from app.tool.contracts import ToolContext


def test_tool_router_selects_free_time_for_meeting_request() -> None:
    """测试会议可用时间请求被确定性路由到只读空闲时间工具。"""
    invocation = AgentToolRouter().select("明天和 wangmin@example.com 约半小时会议，看看可用时间")

    assert invocation is not None
    assert invocation.tool_name == "calendar.find_free_time"
    assert invocation.arguments["duration_minutes"] == 30
    assert invocation.arguments["participants"] == ["wangmin@example.com"]
    assert invocation.arguments["timezone"] == "Asia/Shanghai"


def test_tool_router_selects_event_list_for_calendar_request() -> None:
    """测试普通日程查看请求路由到日程列表工具，而非空闲时间工具。"""
    invocation = AgentToolRouter().select("查看我明天的日程")

    assert invocation is not None
    assert invocation.tool_name == "calendar.list_events"
    assert invocation.arguments["requested_date"] is not None


@pytest.mark.asyncio
async def test_calendar_mcp_client_uses_tools_call_and_returns_structured_content() -> None:
    """测试客户端以 MCP JSON-RPC tools/call 调用并读取 structuredContent。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": captured["id"],
                "result": {
                    "structuredContent": {
                        "events": [{"title": "项目同步", "start_at": "2026-10-05T07:00:00+00:00"}]
                    }
                },
            },
        )

    settings = SimpleNamespace(
        calendar_mcp_url="http://calendar-mcp.test/mcp",
        calendar_mcp_api_key=None,
        calendar_mcp_timeout_seconds=5,
    )
    client = CalendarMCPClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    context = ToolContext(
        tenant_id=UUID("2f744d3e-2a54-4e4f-aec3-bc9f0e2b6966"),
        user_id=UUID("b0071771-d192-4c32-a4e0-7b214eec2be6"),
        run_id=UUID("2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6"),
        connection_id=UUID("734977f1-0a41-4b4f-b9bd-0e960de6f9d2"),
    )

    result = await client.call(
        "calendar.list_events",
        context,
        {
            "start_at": "2026-10-04T00:00:00+00:00",
            "end_at": "2026-10-05T00:00:00+00:00",
            "timezone": "Asia/Shanghai",
        },
    )

    assert captured["method"] == "tools/call"
    assert captured["params"]["name"] == "calendar.list_events"
    assert captured["params"]["_meta"]["aegis_connection_id"] == str(context.connection_id)
    assert result.output_summary["events"][0]["start_at_local"] == "2026-10-05 15:00"
    assert result.output_summary["events"][0]["display_timezone"] == "Asia/Shanghai"
