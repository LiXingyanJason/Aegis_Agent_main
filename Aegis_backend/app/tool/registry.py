"""工具注册表：仅注册过的工具才允许进入网关。"""

from dataclasses import dataclass

from app.tool.contracts import ToolDefinition, ToolError, ToolHandler


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """工具定义与实际处理器的绑定。"""

    definition: ToolDefinition
    handler: ToolHandler


class ToolRegistry:
    """进程内白名单注册表，拒绝 Agent 任意拼接工具名称。"""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, definition: ToolDefinition, handler: ToolHandler) -> None:
        """注册一个名称唯一的工具。"""
        if definition.name in self._tools:
            raise ValueError(f"工具已注册：{definition.name}")
        self._tools[definition.name] = RegisteredTool(definition=definition, handler=handler)

    def get(self, tool_name: str) -> RegisteredTool:
        """返回工具；不存在时以受控错误拒绝调用。"""
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ToolError("TOOL_NOT_REGISTERED", f"未注册工具：{tool_name}")
        return tool
