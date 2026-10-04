"""Calendar MCP Server 的最小环境配置。"""

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class Settings:
    """仅保留当前服务真正使用的配置，避免引入主后端的配置复杂度。"""

    calendar_mcp_api_key: str | None
    calendar_provider: str = "mock"


@lru_cache
def get_settings() -> Settings:
    """从环境变量创建进程级配置实例。"""
    return Settings(
        calendar_mcp_api_key=os.getenv("CALENDAR_MCP_API_KEY") or None,
        calendar_provider=os.getenv("CALENDAR_PROVIDER", "mock"),
    )
