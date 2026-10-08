"""Email MCP Server 的受控客户端。"""

from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from aegis_agent_worker.config.settings import Settings
from aegis_agent_worker.tool.contracts import ToolContext, ToolError, ToolResult


class EmailMCPClient:
    """Worker 使用的受控 Email MCP 客户端，浏览器不可直接调用。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def call(
        self, tool_name: str, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        """按统一 Tool 契约调用 MCP，并生成不含正文的安全展示摘要。"""
        if self._settings.email_mcp_url is None:
            raise ToolError("EMAIL_TOOL_UNAVAILABLE", "邮件工具服务尚未配置")
        if context.connection_id is None:
            raise ToolError("MAIL_CONNECTION_REQUIRED", "尚未连接可读取的工作邮箱")
        headers = {"Accept": "application/json, text/event-stream"}
        api_key = self._settings.email_mcp_api_key
        if api_key is not None and api_key.get_secret_value().strip():
            headers["Authorization"] = f"Bearer {api_key.get_secret_value().strip()}"
        meta = {
            "aegis_connection_id": str(context.connection_id),
            "aegis_tenant_id": str(context.tenant_id),
            "aegis_user_id": str(context.user_id),
            "aegis_run_id": str(context.run_id),
        }
        try:
            async with httpx2.AsyncClient(
                headers=headers, timeout=self._settings.email_mcp_timeout_seconds
            ) as client:
                async with streamable_http_client(
                    str(self._settings.email_mcp_url), http_client=client
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, arguments, meta=meta)
        except Exception as error:
            raise ToolError("EMAIL_TOOL_UNAVAILABLE", "邮件工具服务暂时不可用") from error
        if result.is_error:
            raise ToolError("EMAIL_TOOL_EXECUTION_FAILED", _error_message(result))
        if not isinstance(result.structured_content, dict):
            raise ToolError("EMAIL_TOOL_INVALID_RESPONSE", "邮件工具返回格式无效")
        structured = result.structured_content
        return ToolResult(
            response_payload=structured,
            output_summary=_safe_summary(tool_name, structured),
        )


def _error_message(result: Any) -> str:
    """从 MCP 错误响应提取面向用户的受控文本。"""
    for content in result.content:
        value = getattr(content, "text", None)
        if isinstance(value, str) and value.strip():
            return value[:500]
    return "邮件工具调用失败"


def _safe_summary(tool_name: str, value: dict[str, Any]) -> dict[str, Any]:
    """保留页面预览和审计所需字段，避免把完整邮件正文写入通用工具事件。"""
    if tool_name == "mail.messages.get":
        return {
            key: value[key]
            for key in ("provider_message_id", "sender_name", "sender_email", "subject", "received_at")
            if key in value
        }
    if tool_name == "mail.messages.send":
        return {
            key: value[key]
            for key in ("provider_message_id", "sent_at", "recipients")
            if key in value
        }
    return {key: value[key] for key in list(value)[:20] if key != "body_text"}
