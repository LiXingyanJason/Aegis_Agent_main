"""本地用户与租户映射的数据访问层。"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class LocalUser:
    """已启用的 Aegis 本地用户及所属租户。"""

    id: UUID
    tenant_id: UUID
    email: str
    display_name: str


class UserRepository:
    """以 OIDC issuer 与 subject 映射 Aegis 用户。"""

    async def find_by_oidc(self, session: AsyncSession, issuer: str, subject: str) -> LocalUser | None:
        """查找状态正常的本地用户；已禁用用户或租户不会返回。"""
        result = await session.execute(
            text(
                "SELECT u.id, u.tenant_id, u.email, u.display_name "
                "FROM users u JOIN tenants t ON t.id = u.tenant_id "
                "WHERE u.oidc_issuer = :issuer AND u.oidc_subject = :subject "
                "AND u.status = 'active' AND t.status = 'active'"
            ),
            {"issuer": issuer, "subject": subject},
        )
        row = result.mappings().first()
        if row is None:
            return None
        return LocalUser(id=row["id"], tenant_id=row["tenant_id"], email=row["email"], display_name=row["display_name"])

    async def provision_in_tenant(
        self,
        session: AsyncSession,
        *,
        tenant_name: str,
        issuer: str,
        subject: str,
        email: str,
        display_name: str,
    ) -> LocalUser:
        """在显式指定的开发租户中创建或复用本地用户。"""
        tenant_result = await session.execute(
            text(
                "INSERT INTO tenants (name) VALUES (:tenant_name) "
                "ON CONFLICT (name) DO UPDATE SET updated_at = now() "
                "RETURNING id"
            ),
            {"tenant_name": tenant_name},
        )
        tenant_id = tenant_result.scalar_one()
        user_result = await session.execute(
            text(
                "INSERT INTO users (tenant_id, oidc_issuer, oidc_subject, email, display_name) "
                "VALUES (:tenant_id, :issuer, :subject, :email, :display_name) "
                "ON CONFLICT (oidc_issuer, oidc_subject) DO UPDATE SET "
                "email = EXCLUDED.email, display_name = EXCLUDED.display_name, updated_at = now() "
                "RETURNING id, tenant_id, email, display_name"
            ),
            {
                "tenant_id": tenant_id,
                "issuer": issuer,
                "subject": subject,
                "email": email,
                "display_name": display_name,
            },
        )
        row = user_result.mappings().one()
        return LocalUser(id=row["id"], tenant_id=row["tenant_id"], email=row["email"], display_name=row["display_name"])

