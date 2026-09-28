"""将已验证 OIDC 身份映射为 Aegis 本地用户。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.repository.user_repository import LocalUser, UserRepository
from app.security.auth_security import OIDCIdentity


class UserNotProvisionedError(Exception):
    """外部身份有效，但尚未获得 Aegis 租户成员资格。"""


class IdentityService:
    """处理 OIDC 身份到本地用户、租户的受控映射。"""

    def __init__(self, settings: Settings, users: UserRepository | None = None) -> None:
        self._settings = settings
        self._users = users or UserRepository()

    async def resolve_user(self, session: AsyncSession, identity: OIDCIdentity) -> LocalUser:
        """返回已有用户，或仅在显式开发配置开启时创建用户。"""
        local_user = await self._users.find_by_oidc(session, identity.issuer, identity.subject)
        if local_user is not None:
            return local_user

        if not self._settings.auto_provision_users or not self._settings.default_tenant_name:
            raise UserNotProvisionedError("该 OIDC 用户尚未被分配到 Aegis 租户")

        async with session.begin():
            return await self._users.provision_in_tenant(
                session,
                tenant_name=self._settings.default_tenant_name,
                issuer=identity.issuer,
                subject=identity.subject,
                email=identity.email,
                display_name=identity.display_name,
            )

