"""日历 MCP 工具契约、路由和 SDK Client 适配测试。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr
from aegis_agent_worker.agent.specialists.scheduler_agent import SchedulerAgent
from aegis_agent_worker.tool.calendar import mcp_client as client_module
from aegis_agent_worker.tool.calendar.mcp_client import CalendarMCPClient
from aegis_agent_worker.tool.contracts import ToolContext


def test_scheduler_agent_selects_free_time_for_meeting_request() -> None:
    """测试日程专职 Agent 将会议可用时间请求选择为只读空闲时间工具。"""
    invocation = SchedulerAgent().select_read_tool("明天和 wangmin@example.com 约半小时会议，看看可用时间")

    assert invocation is not None
    assert invocation.tool_name == "calendar.find_free_time"
    assert invocation.arguments["duration_minutes"] == 30
    assert invocation.arguments["participants"] == ["wangmin@example.com"]
    assert invocation.arguments["timezone"] == "Asia/Shanghai"


def test_scheduler_agent_selects_event_list_for_calendar_request() -> None:
    """测试普通日程查看请求选择日程列表工具，而非空闲时间工具。"""
    invocation = SchedulerAgent().select_read_tool("查看我明天的日程")

    assert invocation is not None
    assert invocation.tool_name == "calendar.list_events"
    assert invocation.arguments["requested_date"] is not None


@pytest.mark.asyncio
async def test_calendar_mcp_client_uses_sdk_session_and_returns_structured_content(monkeypatch) -> None:
    """测试客户端通过 MCP SDK 初始化 Session、传递 _meta 并读取 structuredContent。"""
    captured: dict = {}

    class FakeHTTPClient:
        """模拟由主服务控制超时和鉴权头的 HTTP 客户端。"""

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
            return None

    def fake_http_client_factory(headers, timeout):
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeHTTPClient()

    @asynccontextmanager
    async def fake_transport(url, *, http_client):
        """模拟 SDK Streamable HTTP 传输，不依赖真实网络服务。"""
        captured["url"] = url
        captured["http_client"] = http_client
        yield object(), object()

    class FakeClientSession:
        """模拟 MCP SDK ClientSession 的最小交互。"""

        def __init__(self, _read_stream, _write_stream) -> None:
            self.initialized = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
            return None

        async def initialize(self) -> None:
            self.initialized = True

        async def call_tool(self, tool_name, arguments, *, meta):
            assert self.initialized is True
            captured["tool_name"] = tool_name
            captured["arguments"] = arguments
            captured["meta"] = meta
            return SimpleNamespace(
                is_error=False,
                structured_content={
                    "events": [{"title": "项目同步", "start_at": "2026-10-05T07:00:00+00:00"}]
                },
            )

    monkeypatch.setattr(client_module, "streamable_http_client", fake_transport)
    monkeypatch.setattr(client_module, "ClientSession", FakeClientSession)
    settings = SimpleNamespace(
        calendar_mcp_url="http://calendar-mcp.test/mcp",
        # 模拟 `.env` 中 `CALENDAR_MCP_API_KEY=` 的空值；客户端不应发送非法 Bearer 空值请求头。
        calendar_mcp_api_key=SecretStr(""),
        calendar_mcp_timeout_seconds=5,
    )
    client = CalendarMCPClient(settings, http_client_factory=fake_http_client_factory)
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

    assert captured["tool_name"] == "calendar.list_events"
    assert captured["headers"]["Accept"] == "application/json, text/event-stream"
    assert "Authorization" not in captured["headers"]
    assert captured["meta"]["aegis_connection_id"] == str(context.connection_id)
    assert result.output_summary["events"][0]["start_at_local"] == "2026-10-05 15:00"
    assert result.output_summary["events"][0]["display_timezone"] == "Asia/Shanghai"
