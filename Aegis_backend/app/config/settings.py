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

    database_url: str
    database_echo: bool = False
    redis_url: str

    oidc_issuer_url: AnyHttpUrl
    oidc_audience: str
    oidc_client_id: str
    auto_provision_users: bool = False
    default_tenant_name: str | None = None

    model_provider: str
    model_api_base: AnyHttpUrl
    model_api_key: SecretStr
    model_default_name: str
    model_request_timeout_seconds: float = 60.0

    agent_queue_name: str = "aegis:agent-runs"
    # Redis BLPOP 协议只接受整数秒，不能使用 5.0 这类浮点数。
    agent_worker_poll_timeout_seconds: int = 5

    # 用户相对日期（今天、明天等）的解释时区。后续可迁移为租户或用户级偏好。
    app_timezone: str = "Asia/Shanghai"

    # Calendar MCP Server 为内部受信任服务；未配置时仅影响日历意图，不影响普通对话。
    calendar_mcp_url: AnyHttpUrl | None = None
    calendar_mcp_api_key: SecretStr | None = None
    calendar_mcp_timeout_seconds: float = 20.0

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

