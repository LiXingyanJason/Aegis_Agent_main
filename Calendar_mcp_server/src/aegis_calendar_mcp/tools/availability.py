"""日历空闲时间查询工具。"""

from datetime import datetime
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import ValidationError

from aegis_calendar_mcp.schemas.calendar import FindFreeTimeParam
from aegis_calendar_mcp.services.calendar_service import CalendarService
from aegis_calendar_mcp.tools.events import context_from_mcp


def register_availability_tools(mcp: MCPServer, calendar_service: CalendarService) -> None:
    """注册可用时间读取工具。"""

    @mcp.tool(name="calendar.find_free_time", description="查询指定时间范围内可容纳会议时长的空闲时段。仅只读。")
    async def find_free_time(start_at: datetime, end_at: datetime, ctx: Context, duration_minutes: int = 30, participants: list[str] | None = None, timezone: str | None = None, requested_date: str | None = None) -> dict[str, Any]:
        """计算当前用户的可用会议时间。"""
        del timezone, requested_date
        try:
            context = context_from_mcp(ctx)
            param = FindFreeTimeParam(start_at=start_at, end_at=end_at, duration_minutes=duration_minutes, participants=participants or [])
        except (ValidationError, ValueError) as error:
            raise ValueError(str(error)) from error
        return (await calendar_service.find_free_time(context, param)).model_dump(mode="json")
