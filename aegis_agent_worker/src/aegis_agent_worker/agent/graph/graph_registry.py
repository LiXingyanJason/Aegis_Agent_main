"""根图可调用工作流的显式注册表。"""

from collections.abc import Mapping
from typing import Any


class GraphRegistry:
    """保存工作流名称与已编译 LangGraph 子图的映射。"""

    def __init__(self, graphs: Mapping[str, Any] | None = None) -> None:
        self._graphs = dict(graphs or {})

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
