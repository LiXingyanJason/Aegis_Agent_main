from pydantic import ValidationError

from app.config.settings import Settings


def valid_settings() -> dict[str, str]:
    """构造一套完整有效的配置，供各配置校验测试复用。"""
    return {
        "database_url": "postgresql://aegis:secret@localhost:5432/aegis_pa",
        "redis_url": "redis://localhost:6379/0",
        "oidc_issuer_url": "https://id.example.com/realms/aegis",
        "oidc_audience": "aegis-pa-api",
        "oidc_client_id": "aegis-pa-web",
        "model_provider": "openai",
        "model_api_base": "https://api.openai.com/v1",
        "model_api_key": "test-key",
        "model_default_name": "test-model",
    }


def test_settings_normalizes_postgresql_url() -> None:
    """测试普通 PostgreSQL 地址会被转换为 SQLAlchemy 所需的 asyncpg 异步地址，且模型密钥可被正确读取。"""
    settings = Settings(_env_file=None, **valid_settings())
    assert settings.database_async_url == "postgresql+asyncpg://aegis:secret@localhost:5432/aegis_pa"
    assert settings.model_api_key.get_secret_value() == "test-key"


def test_settings_rejects_non_postgresql_database_url() -> None:
    """测试 MySQL 等非 PostgreSQL 地址在获取异步数据库地址时会被拒绝。"""
    values = valid_settings()
    values["database_url"] = "mysql://localhost/aegis_pa"
    settings = Settings(_env_file=None, **values)
    try:
        _ = settings.database_async_url
    except ValueError as error:
        assert "DATABASE_URL" in str(error)
    else:
        raise AssertionError("Expected an invalid database URL to be rejected")


def test_settings_requires_security_and_connection_values() -> None:
    """测试缺少数据库连接或 OIDC 等必填安全配置时，配置对象无法创建。"""
    try:
        Settings(_env_file=None)
    except ValidationError as error:
        assert "database_url" in str(error)
        assert "oidc_issuer_url" in str(error)
    else:
        raise AssertionError("Expected missing required settings to be rejected")

