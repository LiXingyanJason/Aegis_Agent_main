"""Worker 启动时构造默认只读工具注册表和网关。"""

from aegis_agent_worker.config.settings import Settings
from aegis_agent_worker.tool.calendar.handler import CalendarMCPToolHandler
from aegis_agent_worker.tool.calendar.mcp_client import CalendarMCPClient
from aegis_agent_worker.tool.contracts import ToolDefinition
from aegis_agent_worker.tool.gateway import ToolGateway
from aegis_agent_worker.tool.registry import ToolRegistry


def create_default_tool_gateway(settings: Settings) -> ToolGateway:
    """注册日历读工具及只能由已批准流程调用的创建工具。"""
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
    registry.register(
        ToolDefinition(
            name="calendar.create_event",
            version="1.0",
            risk_level="write",
            mcp_server="calendar-mcp",
            description="在用户批准的会议草稿基础上创建日历事件。",
            requires_calendar_connection=True,
        ),
        handler,
    )
    return ToolGateway(registry)
