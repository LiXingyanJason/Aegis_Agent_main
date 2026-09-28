from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from app.common.exceptions import TenantContextError
from app.config.database import TenantContext, get_tenant_context, tenant_transaction


class FakeSession:
    def __init__(self) -> None:
        self.statement = None
        self.parameters = None
        self.entered = False

    @asynccontextmanager
    async def begin(self):
        self.entered = True
        try:
            yield self
        finally:
            self.entered = False

    async def execute(self, statement, parameters):
        self.statement = statement
        self.parameters = parameters


@pytest.mark.asyncio
async def test_tenant_transaction_sets_local_rls_variables_and_context() -> None:
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
    with pytest.raises(TenantContextError):
        get_tenant_context()
