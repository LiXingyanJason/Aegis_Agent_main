"""MCP Server 创建与工具注册。"""

from mcp.server.mcpserver import MCPServer

from aegis_calendar_mcp.config import Settings
from aegis_calendar_mcp.providers.mock import MockCalendarProvider
from aegis_calendar_mcp.services.calendar_service import CalendarService
from aegis_calendar_mcp.tools.availability import register_availability_tools
from aegis_calendar_mcp.tools.events import register_event_tools


def create_server(settings: Settings) -> MCPServer:
    """创建无用户状态的 Calendar MCP Server。"""
    if settings.provider != "mock":
        raise ValueError(f"当前未支持的日历 Provider：{settings.provider}")
    calendar_service = CalendarService(MockCalendarProvider())
    mcp = MCPServer("aegis-calendar-mcp", instructions="Aegis 内部只读日历工具服务；身份、权限和审批由主后端处理。", version="0.1.0")


    register_event_tools(mcp, calendar_service) # 将 list_events 工具注册进入 mcp
    register_availability_tools(mcp, calendar_service) # 将 find_free_time 工具注册进入 mcp

    return mcp
