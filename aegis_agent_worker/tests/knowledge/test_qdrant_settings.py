"""云向量库配置准备不意味着已接入检索，不访问网络。"""
import pytest
from aegis_agent_worker.config.settings import Settings


def configured(**overrides):
    return Settings(_env_file=None, database_url="postgresql://test", redis_url="redis://test",
        model_provider="deepseek", model_api_base="https://test.invalid", model_api_key="test",
        model_default_name="chat", **overrides)


def test_qdrant_is_disabled_by_default():
    assert not configured().qdrant_enabled


@pytest.mark.parametrize("values", [{}, {"qdrant_url": "http://cluster.invalid", "qdrant_api_key": "test"},
    {"qdrant_url": "https://cluster.invalid", "qdrant_api_key": " "}])
def test_enabled_cloud_requires_https_and_nonempty_key(values):
    with pytest.raises(ValueError):
        configured(qdrant_enabled=True, **values)


def test_valid_configuration_keeps_keys_secret():
    config = configured(qdrant_enabled=True, qdrant_url="https://cluster.invalid:6333", qdrant_api_key="test-only-secret")
    assert config.qdrant_url.scheme == "https"
    assert "test-only-secret" not in repr(config)


@pytest.mark.parametrize("values", [{"qdrant_collection": "../unsafe"}, {"qdrant_request_timeout_seconds": 0}])
def test_invalid_collection_or_timeout_is_rejected(values):
    with pytest.raises(ValueError):
        configured(**values)
