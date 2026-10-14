"""Worker 启动时构造默认只读工具注册表和网关。"""

from aegis_agent_worker.config.settings import Settings
from aegis_agent_worker.tool.calendar.handler import CalendarMCPToolHandler
from aegis_agent_worker.tool.calendar.mcp_client import CalendarMCPClient
from aegis_agent_worker.tool.email.handler import EmailMCPToolHandler
from aegis_agent_worker.tool.email.mcp_client import EmailMCPClient
from aegis_agent_worker.tool.contracts import ToolDefinition
from aegis_agent_worker.tool.gateway import ToolGateway
from aegis_agent_worker.tool.registry import ToolRegistry
from aegis_agent_worker.tool.context.handler import CardContextToolHandler
from aegis_agent_worker.agent.contracts.context import CONTEXT_SCHEMAS
from aegis_agent_worker.agent.contracts.knowledge import KnowledgeQueryPlan
from aegis_agent_worker.tool.knowledge.handler import KnowledgeSearchHandler
from aegis_agent_worker.service.knowledge.retrieval_service import RetrievalService


def create_default_tool_gateway(settings: Settings, harness=None, card_context=None, knowledge_retrieval=None) -> ToolGateway:
    """注册日历、邮件 MCP 工具及各自的连接权限要求。"""
    registry = ToolRegistry()
    registry.register(ToolDefinition(name="knowledge.search", version="1.0", risk_level="read",
        mcp_server="aegis-internal", description="查询当前用户已授权知识资料；未配置不返回虚构数据。",
        argument_schema=KnowledgeQueryPlan), KnowledgeSearchHandler(knowledge_retrieval or RetrievalService()))
    if card_context is not None:
        context_handler = CardContextToolHandler(card_context)
        for name, schema in CONTEXT_SCHEMAS.items():
            registry.register(ToolDefinition(name=name, version="1.0", risk_level="read",
                mcp_server="aegis-internal", description="读取当前会话已展示卡片的安全上下文，不能查询邮箱或写入。",
                argument_schema=schema), context_handler)
    handler = CalendarMCPToolHandler(CalendarMCPClient(settings))
    registry.register(
        ToolDefinition(
            name="calendar.list_events",
            version="1.0",
            risk_level="read",
            mcp_server="calendar-mcp",
            description="查询当前用户指定时间范围内的日程。",
            connection_providers=("google_calendar", "outlook_calendar"),
            required_connection_scope="calendar.read",
        ),
        handler,
    )
    registry.register(
        ToolDefinition(
            name="calendar.find_free_time",
            version="1.0",
            risk_level="read",
            mcp_server="calendar-mcp",
            description="查询当前用户在指定范围内的可用时间。",
            connection_providers=("google_calendar", "outlook_calendar"),
            required_connection_scope="calendar.read",
        ),
        handler,
    )
    registry.register(
        ToolDefinition(
            name="calendar.create_event",
            version="1.0",
            risk_level="write",
            mcp_server="calendar-mcp",
            description="在用户批准的会议草稿基础上创建日历事件。",
            connection_providers=("google_calendar", "outlook_calendar"),
            required_connection_scope="calendar.write",
        ),
        handler,
    )
    email_handler = EmailMCPToolHandler(EmailMCPClient(settings))
    registry.register(
        ToolDefinition(
            name="mail.messages.list",
            version="1.0",
            risk_level="read",
            mcp_server="email-mcp",
            description="读取当前用户邮箱的邮件列表快照，不返回完整正文。",
            connection_providers=("gmail", "outlook_mail"),
            required_connection_scope="mail.read",
        ),
        email_handler,
    )
    registry.register(
        ToolDefinition(
            name="mail.messages.get",
            version="1.0",
            risk_level="read",
            mcp_server="email-mcp",
            description="读取当前用户一封已同步邮件的正文与附件元数据。",
            connection_providers=("gmail", "outlook_mail"),
            required_connection_scope="mail.read",
        ),
        email_handler,
    )
    registry.register(
        ToolDefinition(
            name="mail.messages.send",
            version="1.0",
            risk_level="sensitive",
            mcp_server="email-mcp",
            description="发送一封已经用户逐项批准的邮件。",
            connection_providers=("gmail", "outlook_mail"),
            required_connection_scope="mail.send",
        ),
        email_handler,
    )
    return ToolGateway(registry, harness=harness)
