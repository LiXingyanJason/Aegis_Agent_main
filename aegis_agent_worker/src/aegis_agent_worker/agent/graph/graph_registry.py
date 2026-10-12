"""根图可调用工作流的显式注册表。"""

from collections.abc import Mapping
from typing import Any, Protocol
from aegis_agent_worker.agent.contracts.decision import ConversationCapability


class WorkflowRegistrar(Protocol):
    """领域工作流工厂的最小协议；根图不需要知道其具体领域类型。"""

    def register(self, registry: "GraphRegistry") -> None:
        """向根图注册该领域支持的已编译子图。"""


class GraphRegistry:
    """保存工作流名称与已编译 LangGraph 子图的映射。"""

    def __init__(self, graphs: Mapping[str, Any] | None = None) -> None:
        self._graphs = dict(graphs or {})
        self._capabilities: list[ConversationCapability] = []

    def register(self, name: str, graph: Any) -> None:
        """注册一个供根图调用的子图。"""
        if name in self._graphs:
            raise ValueError(f"工作流已注册：{name}")
        self._graphs[name] = graph

    def get(self, name: str) -> Any:
        """返回已注册子图；未知名称不能静默回退。"""
        try:
            return self._graphs[name]
        except KeyError as error:
            raise ValueError(f"未注册的工作流：{name}") from error

    def names(self) -> set[str]:
        """返回已注册名称，供根图只为实际存在的子图创建节点和边。"""
        return set(self._graphs)

    def register_capability(self, capability: ConversationCapability) -> None:
        """只允许已注册的图声明普通对话入口，审批恢复图不自动开放。"""
        if capability.workflow not in self._graphs:
            raise ValueError("能力对应的工作流尚未注册")
        if any((item.domain, item.intent) == (capability.domain, capability.intent)
               for item in self._capabilities):
            raise ValueError("对话能力重复注册")
        self._capabilities.append(capability)

    def capabilities(self) -> tuple[ConversationCapability, ...]:
        return tuple(self._capabilities)
