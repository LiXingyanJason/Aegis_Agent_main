"""供 Controller 使用的认证身份与租户数据库会话依赖。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import Database, TenantContext, tenant_transaction
from app.repository.user_repository import LocalUser
from app.security.auth_security import AuthenticationError, OIDCAuthenticator
from app.service.identity_service import IdentityService, UserNotProvisionedError

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """已认证且已映射到本地租户的请求身份。"""

    user: LocalUser

    @property
    def tenant_context(self) -> TenantContext:
        """转换为数据库 RLS 所需的租户上下文。"""
        return TenantContext(tenant_id=self.user.tenant_id, user_id=self.user.id)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> CurrentUser:
    """验证 Bearer token 并解析为 Aegis 本地用户。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Bearer access token")

    authenticator: OIDCAuthenticator = request.app.state.oidc_authenticator
    identity_service: IdentityService = request.app.state.identity_service
    database: Database = request.app.state.database
    try:
        identity = await authenticator.authenticate(credentials.credentials)
        async with database.session() as session:
            local_user = await identity_service.resolve_user(session, identity)
    except AuthenticationError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)) from error
    except UserNotProvisionedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    return CurrentUser(user=local_user)


async def get_tenant_session(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> AsyncIterator[AsyncSession]:
    """为 Controller 提供已设置 RLS 变量的数据库事务会话。"""
    database: Database = request.app.state.database
    async with database.session() as session:
        async with tenant_transaction(session, current_user.tenant_context):
            yield session
