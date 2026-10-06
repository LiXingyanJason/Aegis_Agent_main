"""日历事件写入 MCP 工具。授权、审批和草稿一致性由主服务保证。"""

from datetime import datetime
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import ValidationError

from aegis_calendar_mcp.schemas.calendar import CreateEventParam
from aegis_calendar_mcp.services.calendar_service import CalendarService
from aegis_calendar_mcp.tools.events import context_from_mcp


def register_write_tools(mcp: MCPServer, calendar_service: CalendarService) -> None:
    """注册唯一的受控日历写入工具。"""

    @mcp.tool(name="calendar.create_event", description="创建已获 Aegis 审批的日历事件。")
    async def create_event(start_at: datetime, end_at: datetime, title: str, idempotency_key: str, ctx: Context, attendees: list[str] | None = None, description: str | None = None) -> dict[str, Any]:
        try:
            context = context_from_mcp(ctx)
            param = CreateEventParam(start_at=start_at, end_at=end_at, title=title, description=description, attendees=attendees or [], idempotency_key=idempotency_key)
        except (ValidationError, ValueError) as error:
            raise ValueError(str(error)) from error
        return (await calendar_service.create_event(context, param)).model_dump(mode="json")
