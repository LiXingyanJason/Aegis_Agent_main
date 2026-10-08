"""Email MCP Server 的进程配置。"""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    """仅保存独立 MCP 工具服务真正需要的运行参数。"""

    provider: str
    transport: str
    host: str
    port: int
    streamable_http_path: str
    allowed_hosts: tuple[str, ...]
    mock_data_path: Path = Path("data/mock_mailbox.json")
    mock_sent_data_path: Path = Path("data/mock_sent_mailbox.json")


@lru_cache
def get_settings() -> Settings:
    """从环境变量读取配置，并拒绝当前不支持的选项。"""
    transport = os.getenv("EMAIL_MCP_TRANSPORT", "streamable-http").lower()
    if transport not in {"stdio", "streamable-http"}:
        raise ValueError("EMAIL_MCP_TRANSPORT 仅支持 stdio 或 streamable-http")
    provider = os.getenv("EMAIL_PROVIDER", "mock").lower()
    if provider != "mock":
        raise ValueError("当前仅支持 EMAIL_PROVIDER=mock")
    port = int(os.getenv("EMAIL_MCP_PORT", "9002"))
    return Settings(
        provider=provider,
        transport=transport,
        host=os.getenv("EMAIL_MCP_HOST", "127.0.0.1"),
        port=port,
        streamable_http_path=os.getenv("EMAIL_MCP_PATH", "/mcp"),
        allowed_hosts=tuple(
            host.strip()
            for host in os.getenv(
                "EMAIL_MCP_ALLOWED_HOSTS",
                f"localhost,localhost:{port},127.0.0.1,127.0.0.1:{port}",
            ).split(",")
            if host.strip()
        ),
        mock_data_path=Path(os.getenv("EMAIL_MOCK_DATA_PATH", "data/mock_mailbox.json")),
        mock_sent_data_path=Path(
            os.getenv("EMAIL_MOCK_SENT_DATA_PATH", "data/mock_sent_mailbox.json")
        ),
    )
