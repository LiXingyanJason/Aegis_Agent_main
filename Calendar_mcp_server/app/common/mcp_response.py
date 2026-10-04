"""统一构造 MCP JSON-RPC 响应。"""

from typing import Any


def success(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """构造 JSON-RPC 成功响应。"""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """构造 JSON-RPC 错误响应，协议错误保持 HTTP 200。"""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
