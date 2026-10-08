"""MCP Server 创建与工具注册。"""

from mcp.server.mcpserver import MCPServer

from aegis_email_mcp.config import Settings
from aegis_email_mcp.providers.mock import MockEmailProvider
from aegis_email_mcp.services.email_service import EmailService
from aegis_email_mcp.tools.messages import register_message_tools


def create_server(settings: Settings) -> MCPServer:
    """创建无用户状态的 Email MCP Server。"""
    if settings.provider != "mock":
        raise ValueError(f"当前未支持的邮件 Provider：{settings.provider}")

    email_service = EmailService(MockEmailProvider(settings.mock_data_path))
    mcp = MCPServer(
        "aegis-email-mcp",
        instructions="Aegis 内部只读邮件工具服务；身份、权限、审计和写操作审批由主后端处理。",
        version="0.1.0",
    )
    register_message_tools(mcp, email_service)
    return mcp
