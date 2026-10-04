"""工具网关：白名单校验、日历连接解析和处理器分发。"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.tool_repository import CalendarConnectionRepository
from app.tool.contracts import ToolContext, ToolError, ToolInvocation, ToolResult
from app.tool.registry import RegisteredTool, ToolRegistry


@dataclass(frozen=True, slots=True)
class PreparedToolInvocation:
    """已通过网关校验并注入连接上下文的工具调用。"""

    registered: RegisteredTool
    context: ToolContext
    arguments: dict


class ToolGateway:
    """隔离 Agent 与具体 MCP Client，集中执行工具安全边界。"""

    def __init__(
        self,
        registry: ToolRegistry,
        connections: CalendarConnectionRepository | None = None,
    ) -> None:
        self._registry = registry
        self._connections = connections or CalendarConnectionRepository()

    def get_definition(self, tool_name: str):
        """读取已注册定义，供调用审计记录使用。"""
        return self._registry.get(tool_name).definition

    async def prepare(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        run_id: UUID,
        invocation: ToolInvocation,
    ) -> PreparedToolInvocation:
        """校验工具白名单，并在需要时解析当前用户的有效日历连接。"""
        registered = self._registry.get(invocation.tool_name)
        if registered.definition.risk_level != "read":
            raise ToolError("TOOL_POLICY_BLOCKED", "当前版本只允许调用只读工具")
        connection_id = None
        if registered.definition.requires_calendar_connection:
            connection = await self._connections.find_active_calendar_connection(
                session, tenant_id=tenant_id, user_id=user_id
            )
            if connection is None:
                raise ToolError("CALENDAR_CONNECTION_REQUIRED", "请先连接具备读取权限的日历账户")
            connection_id = connection.id
        return PreparedToolInvocation(
            registered=registered,
            context=ToolContext(tenant_id=tenant_id, user_id=user_id, run_id=run_id, connection_id=connection_id),
            arguments=dict(invocation.arguments),
        )

    async def invoke(self, prepared: PreparedToolInvocation) -> ToolResult:
        """执行已准备调用；工具名仅在内部注入给共享 MCP 处理器。"""
        arguments = {"__tool_name": prepared.registered.definition.name, **prepared.arguments}
        return await prepared.registered.handler.invoke(prepared.context, arguments)
