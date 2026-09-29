from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from app.common.exceptions import TenantContextError
from app.config.database import (
    TenantContext,
    get_tenant_context,
    register_after_commit,
    tenant_transaction,
)


class FakeSession:
    """模拟异步数据库会话，用于验证租户事务写入的 SQL 与参数，无需连接真实 PostgreSQL。"""

    def __init__(self) -> None:
        """初始化用于记录事务状态、SQL 语句及参数的空会话。"""
        self.statement = None
        self.parameters = None
        self.entered = False

    @asynccontextmanager
    async def begin(self):
        """模拟会话事务的进入与退出，以供租户事务测试断言事务生命周期。"""
        self.entered = True
        try:
            yield self
        finally:
            self.entered = False

    async def execute(self, statement, parameters):
        """记录待执行 SQL 及参数，以验证 RLS 会话变量是否被正确设置。"""
        self.statement = statement
        self.parameters = parameters


@pytest.mark.asyncio
async def test_tenant_transaction_sets_local_rls_variables_and_context() -> None:
    """测试租户事务会开启事务、写入 tenant/user RLS 变量，并在退出后清理 Python 上下文。"""
    session = FakeSession()
    context = TenantContext(
        tenant_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
    )

    async with tenant_transaction(session, context):
        assert get_tenant_context() == context
        assert session.entered is True
        assert "set_config('app.tenant_id'" in str(session.statement)
        assert session.parameters == {
            "tenant_id": "11111111-1111-1111-1111-111111111111",
            "user_id": "22222222-2222-2222-2222-222222222222",
        }

    assert session.entered is False
    with pytest.raises(TenantContextError):
        get_tenant_context()


def test_tenant_context_fails_closed_outside_transaction() -> None:
    """测试未进入租户事务时读取上下文会失败，避免无租户条件访问数据。"""
    with pytest.raises(TenantContextError):
        get_tenant_context()


@pytest.mark.asyncio
async def test_after_commit_callback_runs_only_after_transaction_exits() -> None:
    """测试 Redis 投递等外部动作仅会在数据库事务成功提交后执行。"""
    session = FakeSession()
    context = TenantContext(
        tenant_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
    )
    called: list[str] = []

    async def callback() -> None:
        """模拟事务提交后的 Redis 投递。"""
        assert session.entered is False
        called.append("enqueued")

    async with tenant_transaction(session, context):
        register_after_commit(callback)
        assert called == []

    assert called == ["enqueued"]
