"""异步 SQLAlchemy 引擎、会话及 PostgreSQL RLS 租户事务上下文。"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from aegis_agent_worker.common.exceptions import TenantContextError
from aegis_agent_worker.config.settings import Settings


# frozen=True：创建后不能篡改身份信息 slots=True：节省内存，也避免随意添加属性
@dataclass(frozen=True, slots=True)
class TenantContext:
    """每次租户范围事务使用的已认证身份。"""

    tenant_id: UUID  # 当前租户 ID
    user_id: UUID  #当前已认证用户 ID


_tenant_context: ContextVar[TenantContext | None] = ContextVar("tenant_context", default=None)
_after_commit_callbacks: ContextVar[list[Callable[[], Awaitable[None]]] | None] = ContextVar(
    "after_commit_callbacks", default=None
)
# _tenant_context 本身是一个 ContextVar(存储当前线程运行时的TenantContext)
# : ContextVar[TenantContext | None]表示该变量当前保存的值可以是 TenantContext 或是 None

def get_tenant_context() -> TenantContext:
    """返回当前租户上下文；缺失时以拒绝访问的方式失败。"""
    context = _tenant_context.get()
    if context is None:
        raise TenantContextError("Tenant context is required for tenant-scoped database access")
    return context


def register_after_commit(callback: Callable[[], Awaitable[None]]) -> None:
    """登记仅在当前数据库事务成功提交后才能执行的异步动作。"""
    callbacks = _after_commit_callbacks.get()
    if callbacks is None:
        raise TenantContextError("After-commit callback requires an active tenant transaction")
    callbacks.append(callback) # 写入数据库回调函数，等待数据库提交完毕再执行这些函数


class Database:
    """管理数据库引擎，并创建短生命周期的请求/Worker 会话。"""

    def __init__(self, settings: Settings) -> None:
        self.engine: AsyncEngine = create_async_engine(
            settings.database_async_url,
            echo=settings.database_echo,  # 是否输出 SQL 日志
            pool_pre_ping=True,  # 连接池取出连接前，先检查它是否仍可用
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)  # 异步会话工厂
    # 进入 async with
    #   ↓
    # session_factory() 创建 AsyncSession
    #   ↓
    # yield 把 session 交给调用方
    #   ↓
    # 调用方执行数据库操作
    #   ↓
    # 离开 async with
    #   ↓
    # AsyncSession 自动关闭并归还连接
    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        # 应用关闭时调用，关闭并清空连接池
        await self.engine.dispose()


@asynccontextmanager
async def tenant_transaction(
    session: AsyncSession,
    context: TenantContext,
) -> AsyncIterator[AsyncSession]:
    """
        检查并执行一个 SQL事务
        数据库执行一个事务有安全检查(RLS)，该代码将上下文必要的信息注入到安全检查中。每一个事务，都会过这个安全检查。符合就执行
        检查 1：注入 enant_id user_id ，供 SQLRLS 执行检查(实际检查逻辑在 PostgreSQL中，不在该函数内)
        检查 2：仅根据当前上下文注入 enant_id user_id，执行结束后清空。防止后续事务误用上一个租户或用户的身份
        正常时自动提交，异常时自动回滚；提交后执行 Redis 入队等回调函数
        回调函数：先把一个函数交给别人保存，等某个事件发生后，再由对方调用它
    """

    token: Token[TenantContext | None] = _tenant_context.set(context)
    callbacks: list[Callable[[], Awaitable[None]]] = []
    callback_token = _after_commit_callbacks.set(callbacks)
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
            # 把 session 交给接口Controller使用
            # → 暂停在这里
            # → 等接口函数执行结束
            # → 再回来继续执行 yield 后面的退出逻辑(事务提交)
            # 只有 session.begin() 正常退出、事务已经提交后，才允许投递 Redis 等外部副作用。
        for callback in callbacks:
            await callback() # 真正执行回调函数
    finally:
        _after_commit_callbacks.reset(callback_token)
        _tenant_context.reset(token)

