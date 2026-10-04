"""暴露给 Agent 的只读日历工具处理器。"""

from typing import Any

from app.tool.calendar.mcp_client import CalendarMCPClient
from app.tool.contracts import ToolContext, ToolResult


class CalendarMCPToolHandler:
    """将统一工具契约转交给 Calendar MCP Client。"""

    def __init__(self, client: CalendarMCPClient) -> None:
        self._client = client

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        """工具名称由网关在参数中注入，避免处理器猜测调用目标。"""
        tool_name = arguments.pop("__tool_name")
        return await self._client.call(tool_name, context, arguments)
