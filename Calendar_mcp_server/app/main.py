"""开发期 Calendar MCP Streamable HTTP 的最小 JSON-RPC 服务。"""

import os
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.mock_calendar import MockCalendarService

app = FastAPI(title="Aegis Calendar MCP Server", version="0.1.0")
calendar = MockCalendarService()


class _TimeRange(BaseModel):
    """两个日历工具共享的 UTC 时间范围校验。"""

    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_range(self):
        """拒绝反向、相等或不带时区的时间范围。"""
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("start_at 和 end_at 必须携带时区")
        if self.end_at <= self.start_at:
            raise ValueError("end_at 必须晚于 start_at")
        return self


class ListEventsArguments(_TimeRange):
    """calendar.list_events 的参数。"""


class FindFreeTimeArguments(_TimeRange):
    """calendar.find_free_time 的参数。"""

    duration_minutes: int = Field(default=30, ge=15, le=480)
    participants: list[str] = Field(default_factory=list, max_length=10)


def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """构造 JSON-RPC 成功响应。"""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """构造 JSON-RPC 错误响应，保持 HTTP 200 以符合 RPC 调用语义。"""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _check_service_key(authorization: Annotated[str | None, Header()] = None) -> None:
    """仅当配置服务间密钥时校验 Bearer token；本地 Mock 默认无需密钥。"""
    api_key = os.getenv("CALENDAR_MCP_API_KEY")
    if api_key and authorization != f"Bearer {api_key}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Calendar MCP 服务认证失败")


@app.get("/health", tags=["system"])
async def health_check() -> dict[str, object]:
    """供本地启动和容器健康检查使用。"""
    return {"data": {"status": "ok", "provider": "mock_calendar"}}


@app.post("/mcp", tags=["mcp"])
async def handle_mcp_request(
    request: dict[str, Any],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """处理最小 MCP JSON-RPC initialize、tools/list 和 tools/call 请求。"""
    _check_service_key(authorization)
    request_id = request.get("id")
    if request.get("jsonrpc") != "2.0":
        return _rpc_error(request_id, -32600, "仅支持 JSON-RPC 2.0")
    method = request.get("method")
    if method == "initialize":
        return _rpc_result(
            request_id,
            {
                "protocolVersion": "2025-03-26",
                "serverInfo": {"name": "aegis-calendar-mcp", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            },
        )
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": _tool_definitions()})
    if method != "tools/call":
        return _rpc_error(request_id, -32601, f"不支持的方法：{method}")
    params = request.get("params")
    if not isinstance(params, dict):
        return _rpc_error(request_id, -32602, "tools/call 缺少 params")
    tool_name = params.get("name")
    arguments = params.get("arguments")
    meta = params.get("_meta")
    if not isinstance(arguments, dict) or not isinstance(meta, dict):
        return _rpc_error(request_id, -32602, "工具参数或 Aegis 上下文无效")
    if not _has_valid_aegis_context(meta):
        return _rpc_error(request_id, -32602, "缺少有效 Aegis 连接与身份上下文")
    try:
        if tool_name == "calendar.list_events":
            parsed = ListEventsArguments.model_validate(arguments)
            content = calendar.list_events(parsed.start_at, parsed.end_at)
        elif tool_name == "calendar.find_free_time":
            parsed = FindFreeTimeArguments.model_validate(arguments)
            content = calendar.find_free_time(
                parsed.start_at, parsed.end_at, parsed.duration_minutes, parsed.participants
            )
        else:
            return _rpc_error(request_id, -32601, f"未注册工具：{tool_name}")
    except ValueError as error:
        return _rpc_error(request_id, -32602, str(error))
    return _rpc_result(
        request_id,
        {
            "structuredContent": content,
            "content": [{"type": "text", "text": "Mock 日历查询完成。"}],
            "isError": False,
        },
    )


def _has_valid_aegis_context(meta: dict[str, Any]) -> bool:
    """验证调用方提供的 ID 格式；Mock Server 不访问 Aegis 数据库。"""
    try:
        UUID(str(meta["aegis_connection_id"]))
        UUID(str(meta["aegis_tenant_id"]))
        UUID(str(meta["aegis_user_id"]))
        UUID(str(meta["aegis_run_id"]))
    except (KeyError, ValueError, TypeError):
        return False
    return True


def _tool_definitions() -> list[dict[str, Any]]:
    """返回可被 MCP 客户端发现的两个只读工具 Schema。"""
    common = {
        "type": "object",
        "required": ["start_at", "end_at"],
        "properties": {
            "start_at": {"type": "string", "format": "date-time"},
            "end_at": {"type": "string", "format": "date-time"},
        },
    }
    return [
        {
            "name": "calendar.list_events",
            "description": "查询指定时间范围内当前用户的日程。仅只读。",
            "inputSchema": common,
        },
        {
            "name": "calendar.find_free_time",
            "description": "查询指定时间范围内可容纳会议时长的空闲时段。仅只读。",
            "inputSchema": {
                **common,
                "properties": {
                    **common["properties"],
                    "duration_minutes": {"type": "integer", "minimum": 15, "maximum": 480},
                    "participants": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                },
            },
        },
    ]
