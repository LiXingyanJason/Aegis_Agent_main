"""集中组装 Controller 所需的服务依赖。"""

from functools import lru_cache

from app.config.settings import get_settings
from app.provider.mock_calendar_provider import MockCalendarProvider
from app.service.calendar_service import CalendarService
from app.service.mcp_service import MCPService


@lru_cache
def get_mcp_service() -> MCPService:
    """创建一期 Mock Provider 驱动的 MCP 服务。"""
    settings = get_settings()
    if settings.calendar_provider != "mock":
        raise RuntimeError(f"当前未支持的日历提供商：{settings.calendar_provider}")
    return MCPService(CalendarService(MockCalendarProvider()))
