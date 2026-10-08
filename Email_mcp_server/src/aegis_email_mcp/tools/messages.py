"""邮件读取 MCP 工具。"""

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from aegis_email_mcp.schemas.email import parse_request_context
from aegis_email_mcp.services.email_service import EmailService


def _context_from_mcp(ctx: Context):
    """解析主后端传入的无状态 `_meta` 上下文。"""
    raw_meta = ctx.request_context.meta
    meta = raw_meta.model_dump(mode="json") if hasattr(raw_meta, "model_dump") else raw_meta
    return parse_request_context(meta)


def register_message_tools(mcp: MCPServer, email_service: EmailService) -> None:
    """注册邮件列表和邮件全文两个只读 MCP 工具。"""

    @mcp.tool(name="mail.messages.list", description="获取当前连接邮箱的全部邮件列表快照。仅只读，不返回正文。")
    async def list_messages(ctx: Context) -> dict[str, Any]:
        """读取当前用户的全部邮件列表。"""
        try:
            context = _context_from_mcp(ctx)
            return (await email_service.list_messages(context)).model_dump(mode="json")
        except ValueError as error:
            raise ValueError(str(error)) from error

    @mcp.tool(name="mail.messages.get", description="按 provider_message_id 获取单封邮件的正文与附件元数据。仅只读。")
    async def get_message(provider_message_id: str, ctx: Context) -> dict[str, Any]:
        """读取指定邮件的完整内容，供后续摘要或起草流程使用。"""
        try:
            context = _context_from_mcp(ctx)
            return (
                await email_service.get_message(context, provider_message_id)
            ).model_dump(mode="json")
        except (LookupError, ValueError) as error:
            raise ValueError(str(error)) from error
