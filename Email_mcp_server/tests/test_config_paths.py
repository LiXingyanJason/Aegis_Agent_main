"""Mock 邮箱路径不受启动工作目录影响的回归测试。"""

from aegis_email_mcp.config import PROJECT_ROOT, get_settings, resolve_data_path
from aegis_email_mcp.providers.mock import MockEmailProvider


def test_default_mailbox_loads_from_another_working_directory(monkeypatch, tmp_path):
    """从仓库根目录或 IDE 启动也能读取项目中的真实样例，不自动伪造邮箱。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EMAIL_MOCK_DATA_PATH", raising=False)
    monkeypatch.delenv("EMAIL_MOCK_SENT_DATA_PATH", raising=False)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.mock_data_path == PROJECT_ROOT / "data/mock_mailbox.json"
        assert settings.mock_sent_data_path == PROJECT_ROOT / "data/mock_sent_mailbox.json"
        records = MockEmailProvider(settings.mock_data_path)._load()
        assert records and all("provider_message_id" in record for record in records)
        assert not (tmp_path / "data").exists()
    finally:
        get_settings.cache_clear()


def test_absolute_override_is_preserved_and_relative_override_is_project_scoped(tmp_path):
    """用户指定的绝对路径保持不变，自定义相对路径仍相对于服务项目。"""
    path = tmp_path / "custom-mailbox.json"
    assert resolve_data_path(path) == path.resolve()
    assert resolve_data_path("custom/mailbox.json") == PROJECT_ROOT / "custom/mailbox.json"
