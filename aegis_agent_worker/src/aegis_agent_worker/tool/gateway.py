"""工具网关：白名单校验、日历连接解析和处理器分发。"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aegis_agent_worker.repository.tool_repository import ProviderConnectionRepository
from aegis_agent_worker.tool.contracts import ToolContext, ToolError, ToolInvocation, ToolResult
from aegis_agent_worker.tool.registry import RegisteredTool, ToolRegistry


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
        connections: ProviderConnectionRepository | None = None,
    ) -> None:
        self._registry = registry
        self._connections = connections or ProviderConnectionRepository()

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
        allow_confirmed_write: bool = False,
        required_connection_id: UUID | None = None,
    ) -> PreparedToolInvocation:
        """校验白名单、确认策略与工具声明的外部连接。"""
        registered = self._registry.get(invocation.tool_name)
        if registered.definition.risk_level != "read" and not allow_confirmed_write:
            raise ToolError("TOOL_POLICY_BLOCKED", "该外部写入操作尚未获得用户确认")
        connection_id = None
        if registered.definition.connection_providers:
            connection = await self._connections.find_active_connection(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                providers=registered.definition.connection_providers,
                required_scope=registered.definition.required_connection_scope,
                required_connection_id=required_connection_id,
            )
            if connection is None:
                if registered.definition.required_connection_scope == "mail.send":
                    message = "请先连接具备发送权限的工作邮箱"
                elif registered.definition.required_connection_scope == "mail.read":
                    message = "请先连接具备读取权限的工作邮箱"
                else:
                    message = "请先连接具备所需权限的日历账户"
                prefix = "MAIL" if registered.definition.required_connection_scope and registered.definition.required_connection_scope.startswith("mail.") else "CALENDAR"
                raise ToolError(f"{prefix}_CONNECTION_REQUIRED", message)
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
