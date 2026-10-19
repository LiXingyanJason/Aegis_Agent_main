"""Agent Worker 的独立运行时配置。"""

from functools import lru_cache
from pathlib import Path

from pydantic import AnyHttpUrl, SecretStr, Field, model_validator
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
    session_memory_enabled: bool = False  # 7.2有界工作索引缓存，独立开关灰度
    knowledge_query_enabled: bool = False  # 6.4真实检索显式开启；不将Mock能力提供给主Agent
    knowledge_lexical_fallback_allowed: bool = False  # 显式允许Qdrant服务故障时仅全文检索；不绕过Embedding治理
    knowledge_index_queue_name: str = "aegis:knowledge-index-jobs"
    knowledge_storage_root: str = str(Path(__file__).resolve().parents[4]/"data"/"knowledge-sources")
    knowledge_max_source_bytes: int = Field(default=262144,ge=1024,le=1048576)
    knowledge_index_lease_seconds: int = Field(default=180,ge=120,le=600)
    knowledge_index_recovery_seconds: int = Field(default=30,ge=10,le=300)
    knowledge_index_max_claims: int = Field(default=3,ge=1,le=10)
    embedding_enabled: bool = False
    embedding_provider: str | None = Field(default=None, max_length=64)
    embedding_api_base: AnyHttpUrl | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str | None = Field(default=None, max_length=128)
    embedding_model_version: str | None = Field(default=None, max_length=64)
    embedding_dimensions: int = Field(default=1536, ge=1, le=2000)
    embedding_send_dimensions: bool = False
    embedding_request_timeout_seconds: float = Field(default=30, gt=0, le=120)
    embedding_max_batch_items: int = Field(default=10,ge=1,le=16)  # 阿里云v4最多10条，独立于旧Policy上限
    embedding_external_queries_allowed: bool = False
    qdrant_enabled: bool = False  # 6.3写入与6.4只读检索，密钥分别配置
    qdrant_url: AnyHttpUrl | None = None
    qdrant_api_key: SecretStr | None = None
    qdrant_write_api_key: SecretStr | None = None
    qdrant_collection: str = Field(default="aegis_knowledge_v1", pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    qdrant_request_timeout_seconds: float = Field(default=10, gt=0, le=60)

    @model_validator(mode="after")
    def validate_qdrant(self):
        if self.qdrant_enabled and (not self.qdrant_url or self.qdrant_url.scheme != "https"
            or not self.qdrant_api_key or not self.qdrant_api_key.get_secret_value().strip()):
            raise ValueError("启用 Qdrant Cloud 必须配置 HTTPS 地址和数据库 API Key")
        return self

    @model_validator(mode="after")
    def validate_embedding(self):
        if self.embedding_enabled:
            if self.embedding_provider not in {"openai", "openai_compatible"} or not all((
                self.embedding_api_base, self.embedding_api_key and self.embedding_api_key.get_secret_value().strip(),
                self.embedding_model and self.embedding_model.strip(),
                self.embedding_model_version and self.embedding_model_version.strip())):
                raise ValueError("启用 Embedding 必须单独配置兼容供应商、地址、密钥、模型及版本")
        return self

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
