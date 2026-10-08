"""带类型校验的环境配置；其他模块不得直接读取环境变量。"""

from functools import lru_cache

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从环境变量及可选 `.env` 文件加载的运行时配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Aegis PA API"
    app_env: str = "development"

    # 最小可观测性配置。默认只生成 Trace ID 并写入结构化日志；需要在终端查看完整 Span 时再显式开启控制台导出。
    otel_enabled: bool = True
    otel_service_name: str = "aegis-pa-api"
    otel_console_exporter: bool = False

    database_url: str
    database_echo: bool = False
    redis_url: str

    oidc_issuer_url: AnyHttpUrl
    oidc_audience: str
    oidc_client_id: str
    auto_provision_users: bool = False
    default_tenant_name: str | None = None

    agent_queue_name: str = "aegis:agent-runs"

    # 邮件列表同步由主后端直接调用；摘要和起草任务随后由独立 Agent Worker 调用。
    email_mcp_url: AnyHttpUrl | None = None
    email_mcp_api_key: SecretStr | None = None
    email_mcp_timeout_seconds: float = 20.0

    @property
    def oidc_issuer(self) -> str:
        """返回不带末尾斜杠的 issuer，避免令牌 `iss` 比较出现形式差异。"""
        return str(self.oidc_issuer_url).rstrip("/")

    @property
    def database_async_url(self) -> str:
        """ 返回 SQLAlchemy 异步 PostgreSQL 地址，并提前拒绝其他数据库驱动。
            根据普通 PostgreSQL 连接串，生成 SQLAlchemy 异步驱动 asyncpg 所需的地址 """
        if not self.database_url.startswith(("postgresql+asyncpg://", "postgresql://")):
            raise ValueError("DATABASE_URL must use postgresql:// or postgresql+asyncpg://")
        return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)


# @lru_cache 同一个进程只创建一次Settings()
@lru_cache
def get_settings() -> Settings:
    """每个进程返回一个不可变的配置实例。"""
    return Settings()

