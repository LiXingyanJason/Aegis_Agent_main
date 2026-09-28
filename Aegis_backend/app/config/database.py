"""异步 SQLAlchemy 引擎、会话及 PostgreSQL RLS 租户事务上下文。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.common.exceptions import TenantContextError
from app.config.settings import Settings


@dataclass(frozen=True, slots=True)
class TenantContext:
    """每次租户范围事务使用的已认证身份。"""

    tenant_id: UUID
    user_id: UUID


_tenant_context: ContextVar[TenantContext | None] = ContextVar("tenant_context", default=None)


def get_tenant_context() -> TenantContext:
    """返回当前租户上下文；缺失时以拒绝访问的方式失败。"""
    context = _tenant_context.get()
    if context is None:
        raise TenantContextError("Tenant context is required for tenant-scoped database access")
    return context


class Database:
    """管理数据库引擎，并创建短生命周期的请求/Worker 会话。"""

    def __init__(self, settings: Settings) -> None:
        self.engine: AsyncEngine = create_async_engine(
            settings.database_async_url,
            echo=settings.database_echo,
            pool_pre_ping=True,
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()


@asynccontextmanager
async def tenant_transaction(
    session: AsyncSession,
    context: TenantContext,
) -> AsyncIterator[AsyncSession]:
    """开启事务并设置仅在当前事务有效的 PostgreSQL RLS 变量。

    `set_config(..., true)` 会在提交或回滚后清除变量，避免连接回到
    SQLAlchemy 连接池后遗留上一个租户的上下文。
    """

    token: Token[TenantContext | None] = _tenant_context.set(context)
    try:
        async with session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.tenant_id', :tenant_id, true), "
                    "set_config('app.user_id', :user_id, true)"
                ),
                {"tenant_id": str(context.tenant_id), "user_id": str(context.user_id)},
            )
            yield session
    finally:
        _tenant_context.reset(token)

