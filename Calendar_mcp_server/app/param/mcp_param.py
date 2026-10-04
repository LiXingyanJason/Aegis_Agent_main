"""MCP JSON-RPC 请求参数模型。"""

from typing import Any

from pydantic import BaseModel


class MCPRequestParam(BaseModel):
    """统一接收 MCP initialize、tools/list、tools/call 请求。"""

    jsonrpc: str
    id: Any = None
    method: str
    params: dict[str, Any] | None = None
