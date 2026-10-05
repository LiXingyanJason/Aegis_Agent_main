"""Calendar MCP Server 的进程配置。"""

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class Settings:
    """仅保存独立 MCP 工具服务真正需要的运行参数。"""

    provider: str
    transport: str
    host: str
    port: int
    streamable_http_path: str
    allowed_hosts: tuple[str, ...]


@lru_cache
def get_settings() -> Settings:
    """从环境变量读取配置，并拒绝当前不支持的传输方式。"""
    transport = os.getenv("CALENDAR_MCP_TRANSPORT", "streamable-http").lower()
    if transport not in {"stdio", "streamable-http"}:
        raise ValueError("CALENDAR_MCP_TRANSPORT 仅支持 stdio 或 streamable-http")
    provider = os.getenv("CALENDAR_PROVIDER", "mock").lower()
    if provider != "mock":
        raise ValueError("当前仅支持 CALENDAR_PROVIDER=mock")
    return Settings(
        provider=provider,
        transport=transport,
        host=os.getenv("CALENDAR_MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("CALENDAR_MCP_PORT", "9001")),
        streamable_http_path=os.getenv("CALENDAR_MCP_PATH", "/mcp"),
        allowed_hosts=tuple(
            host.strip()
            for host in os.getenv(
                "CALENDAR_MCP_ALLOWED_HOSTS",
                "localhost,localhost:9001,127.0.0.1,127.0.0.1:9001",
            ).split(",")
            if host.strip()
        ),
    )
