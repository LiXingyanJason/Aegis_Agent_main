"""Calendar MCP Server 的最小 HTTP JSON-RPC 客户端。"""

from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.config.settings import Settings
from app.tool.contracts import ToolContext, ToolError, ToolResult


class CalendarMCPClient:
    """调用受信任 Calendar MCP Server 的 `tools/call` 方法。"""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    async def call(
        self, tool_name: str, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        """按统一 MCP JSON-RPC 载荷调用只读日历工具。"""
        if self._settings.calendar_mcp_url is None:
            raise ToolError("TOOL_UNAVAILABLE", "日历工具服务尚未配置")
        if context.connection_id is None:
            raise ToolError("CALENDAR_CONNECTION_REQUIRED", "尚未连接可用日历")

        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._settings.calendar_mcp_api_key is not None:
            headers["Authorization"] = (
                f"Bearer {self._settings.calendar_mcp_api_key.get_secret_value()}"
            )
        request_payload = {
            "jsonrpc": "2.0",
            "id": str(uuid4()),
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
                "_meta": {
                    "aegis_connection_id": str(context.connection_id),
                    "aegis_tenant_id": str(context.tenant_id),
                    "aegis_user_id": str(context.user_id),
                    "aegis_run_id": str(context.run_id),
                },
            },
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._settings.calendar_mcp_timeout_seconds)
        try:
            response = await client.post(
                str(self._settings.calendar_mcp_url), headers=headers, json=request_payload
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ToolError("TOOL_UNAVAILABLE", "日历工具服务暂时不可用") from error
        finally:
            if owns_client:
                await client.aclose()

        if not isinstance(payload, dict):
            raise ToolError("TOOL_INVALID_RESPONSE", "日历工具服务返回格式无效")
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            raise ToolError("TOOL_EXECUTION_FAILED", str(message or "日历工具调用失败"))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ToolError("TOOL_INVALID_RESPONSE", "日历工具服务未返回 result")
        structured = result.get("structuredContent", result)
        if not isinstance(structured, dict):
            raise ToolError("TOOL_INVALID_RESPONSE", "日历工具结果必须为 JSON 对象")
        return ToolResult(
            response_payload=structured,
            output_summary=_safe_summary(structured, arguments.get("timezone")),
        )


def _safe_summary(value: dict[str, Any], timezone_name: Any = None) -> dict[str, Any]:
    """限制可展示的响应深度和长度，避免前端接收原始大载荷。"""
    timezone = _resolve_timezone(timezone_name)

    def trim(item: Any, depth: int = 0) -> Any:
        if depth >= 4:
            return "…"
        if isinstance(item, str):
            return item[:500]
        if isinstance(item, list):
            return [trim(child, depth + 1) for child in item[:20]]
        if isinstance(item, dict):
            summarized = {str(key): trim(child, depth + 1) for key, child in list(item.items())[:30]}
            _add_local_time_fields(summarized, timezone)
            return summarized
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return str(item)[:500]

    return trim(value)


def _resolve_timezone(timezone_name: Any) -> ZoneInfo | None:
    """从工具参数读取展示时区；错误配置不阻断可用的原始时间结果。"""
    if not isinstance(timezone_name, str):
        return None
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return None


def _add_local_time_fields(value: dict[str, Any], timezone: ZoneInfo | None) -> None:
    """为日程和空闲时间补充本地可读时间，原始 ISO 时间仍完整保留。"""
    if timezone is None:
        return
    for key in ("start_at", "end_at"):
        raw_value = value.get(key)
        if not isinstance(raw_value, str):
            continue
        try:
            instant = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if instant.tzinfo is not None:
            value[f"{key}_local"] = instant.astimezone(timezone).strftime("%Y-%m-%d %H:%M")
    if "start_at_local" in value:
        value["display_timezone"] = timezone.key
