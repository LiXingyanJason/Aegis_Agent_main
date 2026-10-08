"""Email MCP Server 的最小 Streamable HTTP 客户端。"""

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, Field, ValidationError

from app.config.settings import Settings


class EmailMCPError(RuntimeError):
    """邮件工具服务未配置、不可用或返回非法数据时抛出。"""


class EmailMCPMessage(BaseModel):
    """主后端同步邮件快照时允许接收的最小安全字段。"""

    provider_message_id: str = Field(min_length=1, max_length=512)
    provider_thread_id: str | None = Field(default=None, max_length=512)
    sender_name: str | None = Field(default=None, max_length=256)
    sender_email: str = Field(min_length=1, max_length=320)
    subject: str = Field(min_length=1, max_length=998)
    received_at: datetime
    source_version: str = Field(min_length=1, max_length=128)
    list_preview: str | None = Field(default=None, max_length=1000)
    attachment_count: int = Field(ge=0)


class EmailMCPAttachment(BaseModel):
    """邮件详情可展示的附件安全元数据。"""

    file_name: str = Field(min_length=1, max_length=512)
    content_type: str = Field(min_length=1, max_length=256)
    size_bytes: int = Field(ge=0)


class EmailMCPMessageDetail(EmailMCPMessage):
    """Email MCP 的单封邮件详情，不允许携带可执行内容。"""

    recipients: list[str]
    body_text: str
    attachments: list[EmailMCPAttachment]


class EmailMCPClient:
    """以 MCP SDK 调用邮件列表工具，浏览器不会直接接触该内部服务。"""

    def __init__(
        self,
        settings: Settings,
        http_client_factory: Callable[[dict[str, str], float], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._http_client_factory = http_client_factory

    async def list_messages(
        self,
        *,
        connection_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
    ) -> list[EmailMCPMessage]:
        """读取当前授权连接的全部邮件元数据，不请求或返回正文。"""
        if self._settings.email_mcp_url is None:
            raise EmailMCPError("邮件工具服务尚未配置")

        headers = {"Accept": "application/json, text/event-stream"}
        api_key = self._settings.email_mcp_api_key
        if api_key is not None:
            api_key_value = api_key.get_secret_value().strip()
            if api_key_value:
                headers["Authorization"] = f"Bearer {api_key_value}"

        request_meta = {
            "aegis_connection_id": str(connection_id),
            "aegis_tenant_id": str(tenant_id),
            "aegis_user_id": str(user_id),
        }
        try:
            timeout = self._settings.email_mcp_timeout_seconds
            http_client = (
                self._http_client_factory(headers, timeout)
                if self._http_client_factory is not None
                else httpx2.AsyncClient(headers=headers, timeout=timeout)
            )
            async with http_client:
                async with streamable_http_client(
                    str(self._settings.email_mcp_url), http_client=http_client
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            "mail.messages.list", {}, meta=request_meta
                        )
        except Exception as error:
            raise EmailMCPError("邮件工具服务暂时不可用") from error

        if result.is_error:
            raise EmailMCPError(_tool_error_message(result))
        structured = result.structured_content
        if not isinstance(structured, dict) or not isinstance(structured.get("messages"), list):
            raise EmailMCPError("邮件工具结果缺少 messages 列表")
        try:
            return [EmailMCPMessage.model_validate(item) for item in structured["messages"]]
        except ValidationError as error:
            raise EmailMCPError("邮件工具返回的元数据格式无效") from error

    async def get_message(
        self,
        *,
        connection_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        provider_message_id: str,
    ) -> EmailMCPMessageDetail:
        """按外部邮件 ID 读取单封正文，仅供用户明确打开详情时使用。"""
        structured = await self._call_tool(
            "mail.messages.get",
            {"provider_message_id": provider_message_id},
            connection_id=connection_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        try:
            return EmailMCPMessageDetail.model_validate(structured)
        except ValidationError as error:
            raise EmailMCPError("邮件工具返回的详情格式无效") from error

    async def _call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        connection_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        """执行一次 Email MCP 调用并返回其结构化内容。"""
        if self._settings.email_mcp_url is None:
            raise EmailMCPError("邮件工具服务尚未配置")
        headers = {"Accept": "application/json, text/event-stream"}
        api_key = self._settings.email_mcp_api_key
        if api_key is not None:
            api_key_value = api_key.get_secret_value().strip()
            if api_key_value:
                headers["Authorization"] = f"Bearer {api_key_value}"
        request_meta = {
            "aegis_connection_id": str(connection_id),
            "aegis_tenant_id": str(tenant_id),
            "aegis_user_id": str(user_id),
        }
        try:
            timeout = self._settings.email_mcp_timeout_seconds
            http_client = (
                self._http_client_factory(headers, timeout)
                if self._http_client_factory is not None
                else httpx2.AsyncClient(headers=headers, timeout=timeout)
            )
            async with http_client:
                async with streamable_http_client(
                    str(self._settings.email_mcp_url), http_client=http_client
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, arguments, meta=request_meta)
        except Exception as error:
            raise EmailMCPError("邮件工具服务暂时不可用") from error
        if result.is_error:
            raise EmailMCPError(_tool_error_message(result))
        if not isinstance(result.structured_content, dict):
            raise EmailMCPError("邮件工具返回格式无效")
        return result.structured_content


def _tool_error_message(result: Any) -> str:
    """从 MCP 响应中提取可安全返回给页面的错误摘要。"""
    for content in result.content:
        text = getattr(content, "text", None)
        if isinstance(text, str) and text.strip():
            return text[:500]
    return "邮件工具调用失败"
