"""日历事件查询工具。"""

from datetime import datetime
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import ValidationError

from aegis_calendar_mcp.schemas.calendar import ListEventsParam, parse_request_context
from aegis_calendar_mcp.services.calendar_service import CalendarService


def context_from_mcp(ctx: Context):
    """解析主后端传入的无状态 `_meta` 上下文。"""
    raw_meta = ctx.request_context.meta
    meta = raw_meta.model_dump(mode="json") if hasattr(raw_meta, "model_dump") else raw_meta
    return parse_request_context(meta)


def register_event_tools(mcp: MCPServer, calendar_service: CalendarService) -> None:
    """注册日历事件读取工具。"""

    @mcp.tool(name="calendar.list_events", description="查询指定时间范围内当前用户的日程。仅只读。")
    async def list_events(start_at: datetime, end_at: datetime, ctx: Context, timezone: str | None = None, requested_date: str | None = None) -> dict[str, Any]:
        """查询当前用户在指定时间范围内的日程。"""
        del timezone, requested_date
        try:
            context = context_from_mcp(ctx)
            param = ListEventsParam(start_at=start_at, end_at=end_at)
        except (ValidationError, ValueError) as error:
            raise ValueError(str(error)) from error
        return (await calendar_service.list_events(context, param)).model_dump(mode="json")
