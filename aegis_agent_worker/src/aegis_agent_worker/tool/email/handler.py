"""将统一工具契约转交给 Email MCP Client 的处理器。"""

from typing import Any

from aegis_agent_worker.tool.contracts import ToolContext, ToolResult
from aegis_agent_worker.tool.email.mcp_client import EmailMCPClient


class EmailMCPToolHandler:
    """统一 Tool Gateway 与 Email MCP SDK 客户端之间的领域适配器。"""

    def __init__(self, client: EmailMCPClient) -> None:
        self._client = client

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        """读取网关内部注入的工具名，禁止 Agent 直接控制 MCP 调用目标。"""
        tool_name = arguments.pop("__tool_name")
        return await self._client.call(tool_name, context, arguments)
