"""Agent 与外部工具之间的统一调用契约。"""

from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID


RiskLevel = Literal["read", "write", "sensitive"]


class ToolError(RuntimeError):
    """工具不可用、调用失败或返回不符合契约时抛出的受控错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """一个允许 Agent 调用的工具声明，作为注册与安全校验的权威来源。"""

    name: str
    version: str
    risk_level: RiskLevel
    mcp_server: str
    description: str
    requires_calendar_connection: bool = False


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Agent 对某个已注册工具发起的一次结构化调用请求。"""

    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolContext:
    """工具执行所需的受控身份与运行上下文，浏览器不得自行构造。"""

    tenant_id: UUID
    user_id: UUID
    run_id: UUID
    connection_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """工具执行成功后的原始结果与可安全展示的摘要。"""

    response_payload: dict[str, Any]
    output_summary: dict[str, Any]


class ToolHandler(Protocol):
    """所有工具处理器都必须实现的统一异步调用接口。"""

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        """使用受控上下文执行调用并返回结构化结果。"""
