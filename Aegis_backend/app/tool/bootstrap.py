"""Worker 启动时构造默认只读工具注册表和网关。"""

from app.config.settings import Settings
from app.tool.calendar.handler import CalendarMCPToolHandler
from app.tool.calendar.mcp_client import CalendarMCPClient
from app.tool.contracts import ToolDefinition
from app.tool.gateway import ToolGateway
from app.tool.registry import ToolRegistry


def create_default_tool_gateway(settings: Settings) -> ToolGateway:
    """注册一期允许的两个只读 Calendar MCP 工具。"""
    registry = ToolRegistry()
    handler = CalendarMCPToolHandler(CalendarMCPClient(settings))
    registry.register(
        ToolDefinition(
            name="calendar.list_events",
            version="1.0",
            risk_level="read",
            mcp_server="calendar-mcp",
            description="查询当前用户指定时间范围内的日程。",
            requires_calendar_connection=True,
        ),
        handler,
    )
    registry.register(
        ToolDefinition(
            name="calendar.find_free_time",
            version="1.0",
            risk_level="read",
            mcp_server="calendar-mcp",
            description="查询当前用户在指定范围内的可用时间。",
            requires_calendar_connection=True,
        ),
        handler,
    )
    return ToolGateway(registry)
