"""Agent Worker 的独立运行时配置。"""

from functools import lru_cache

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """仅加载 Worker 执行任务所需的环境变量，不包含 OIDC 或 HTTP API 配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Aegis Agent Worker"
    app_env: str = "development"

    otel_enabled: bool = True
    otel_service_name: str = "aegis-agent-worker"
    otel_console_exporter: bool = False

    database_url: str
    database_echo: bool = False
    redis_url: str

    model_provider: str
    model_api_base: AnyHttpUrl
    model_api_key: SecretStr
    model_default_name: str
    model_request_timeout_seconds: float = 60.0

    agent_queue_name: str = "aegis:agent-runs"
    agent_worker_poll_timeout_seconds: int = 5
    app_timezone: str = "Asia/Shanghai"

    calendar_mcp_url: AnyHttpUrl | None = None
    calendar_mcp_api_key: SecretStr | None = None
    calendar_mcp_timeout_seconds: float = 20.0

    email_mcp_url: AnyHttpUrl | None = None
    email_mcp_api_key: SecretStr | None = None
    email_mcp_timeout_seconds: float = 20.0

    @property
    def database_async_url(self) -> str:
        """将 PostgreSQL URL 转换为 SQLAlchemy asyncpg 连接 URL。"""
        if not self.database_url.startswith(("postgresql+asyncpg://", "postgresql://")):
            raise ValueError("DATABASE_URL must use postgresql:// or postgresql+asyncpg://")
        return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    @property
    def database_psycopg_url(self) -> str:
        """将 SQLAlchemy URL 转为 LangGraph Checkpoint/psycopg 可识别的标准 URL。"""
        if not self.database_url.startswith(("postgresql+asyncpg://", "postgresql://")):
            raise ValueError("DATABASE_URL must use postgresql:// or postgresql+asyncpg://")
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    """每个 Worker 进程仅构造一份配置。"""
    return Settings()
