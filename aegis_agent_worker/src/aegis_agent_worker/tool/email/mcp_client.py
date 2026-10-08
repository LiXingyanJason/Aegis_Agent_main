"""Email MCP Server 的受控只读客户端。"""

from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from aegis_agent_worker.config.settings import Settings
from aegis_agent_worker.tool.contracts import ToolContext, ToolError


class EmailMCPClient:
    """Worker 使用的受控 Email MCP 客户端，浏览器不可直接调用。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def get_message(self, context: ToolContext, provider_message_id: str) -> dict[str, Any]:
        """读取一封受当前连接约束的邮件正文与附件元数据。"""
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
                        result = await session.call_tool(
                            "mail.messages.get", {"provider_message_id": provider_message_id}, meta=meta
                        )
        except Exception as error:
            raise ToolError("EMAIL_TOOL_UNAVAILABLE", "邮件工具服务暂时不可用") from error
        if result.is_error:
            raise ToolError("EMAIL_TOOL_EXECUTION_FAILED", _error_message(result))
        if not isinstance(result.structured_content, dict):
            raise ToolError("EMAIL_TOOL_INVALID_RESPONSE", "邮件工具返回格式无效")
        return result.structured_content

    async def send_message(
        self,
        context: ToolContext,
        *,
        to: list[str],
        cc: list[str],
        subject: str,
        body: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """调用已批准的发送工具；幂等键由 Aegis 外部动作记录生成。"""
        return await self._call_tool(
            "mail.messages.send",
            {
                "to": to,
                "cc": cc,
                "subject": subject,
                "body": body,
                "idempotency_key": idempotency_key,
            },
            context,
        )

    async def _call_tool(
        self, tool_name: str, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        """统一执行一次 Email MCP 调用并校验结构化响应。"""
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
        return result.structured_content


def _error_message(result: Any) -> str:
    """从 MCP 错误响应提取面向用户的受控文本。"""
    for content in result.content:
        value = getattr(content, "text", None)
        if isinstance(value, str) and value.strip():
            return value[:500]
    return "邮件工具调用失败"
