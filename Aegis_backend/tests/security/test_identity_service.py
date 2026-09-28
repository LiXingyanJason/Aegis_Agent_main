from uuid import UUID

import pytest

from app.config.settings import Settings
from app.repository.user_repository import LocalUser
from app.security.auth_security import OIDCIdentity
from app.service.identity_service import IdentityService, UserNotProvisionedError


def make_settings(**overrides: object) -> Settings:
    """构造身份映射测试使用的完整配置。"""
    values: dict[str, object] = {
        "database_url": "postgresql://aegis:secret@localhost:5432/aegis_pa",
        "redis_url": "redis://localhost:6379/0",
        "oidc_issuer_url": "http://127.0.0.1:8080/realms/aegis",
        "oidc_audience": "aegis-pa-api",
        "oidc_client_id": "aegis-pa-web",
        "model_provider": "openai",
        "model_api_base": "https://api.openai.com/v1",
        "model_api_key": "test-key",
        "model_default_name": "test-model",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_identity() -> OIDCIdentity:
    """构造已通过 JWT 验证的测试 OIDC 身份。"""
    return OIDCIdentity(
        issuer="http://127.0.0.1:8080/realms/aegis",
        subject="keycloak-user-id",
        email="jason@example.com",
        display_name="Jason Li",
        claims={},
    )


class FakeUsers:
    """模拟用户仓储，验证身份服务的映射与自动创建决策。"""

    def __init__(self, existing: LocalUser | None = None) -> None:
        """指定查询时是否返回已有本地用户。"""
        self.existing = existing
        self.provisioned = False

    async def find_by_oidc(self, session, issuer: str, subject: str) -> LocalUser | None:
        """模拟按 issuer 与 subject 查询用户。"""
        return self.existing

    async def provision_in_tenant(self, session, **kwargs) -> LocalUser:
        """模拟在开发租户中创建用户，并记录是否发生创建。"""
        self.provisioned = True
        return LocalUser(
            id=UUID("11111111-1111-1111-1111-111111111111"),
            tenant_id=UUID("22222222-2222-2222-2222-222222222222"),
            email=kwargs["email"],
            display_name=kwargs["display_name"],
        )


class FakeSession:
    """模拟身份映射创建时使用的异步事务会话。"""

    def begin(self):
        """返回可异步进入的空事务上下文。"""
        return _FakeTransaction()


class _FakeTransaction:
    """模拟 SQLAlchemy 事务上下文。"""

    async def __aenter__(self):
        """进入模拟事务。"""
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        """退出模拟事务。"""
        return False


@pytest.mark.asyncio
async def test_existing_oidc_identity_resolves_to_local_user() -> None:
    """测试已分配租户的 OIDC 身份会直接返回本地用户，不触发自动创建。"""
    existing = LocalUser(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        tenant_id=UUID("22222222-2222-2222-2222-222222222222"),
        email="jason@example.com",
        display_name="Jason Li",
    )
    users = FakeUsers(existing)
    service = IdentityService(make_settings(), users)

    resolved = await service.resolve_user(FakeSession(), make_identity())

    assert resolved == existing
    assert users.provisioned is False


@pytest.mark.asyncio
async def test_unknown_oidc_identity_is_rejected_when_auto_provisioning_is_disabled() -> None:
    """测试生产默认设置会拒绝未被预先分配租户的外部身份。"""
    service = IdentityService(make_settings(), FakeUsers())

    with pytest.raises(UserNotProvisionedError):
        await service.resolve_user(FakeSession(), make_identity())


@pytest.mark.asyncio
async def test_unknown_oidc_identity_is_provisioned_only_with_explicit_development_setting() -> None:
    """测试仅显式开启自动创建并指定开发租户时，未知身份才会被创建。"""
    users = FakeUsers()
    service = IdentityService(
        make_settings(auto_provision_users=True, default_tenant_name="aegis-dev"),
        users,
    )

    resolved = await service.resolve_user(FakeSession(), make_identity())

    assert users.provisioned is True
    assert resolved.email == "jason@example.com"
