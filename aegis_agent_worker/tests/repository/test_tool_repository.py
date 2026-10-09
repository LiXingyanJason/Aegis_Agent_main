"""外部服务连接查询的数据访问测试。"""

from uuid import UUID

import pytest

from aegis_agent_worker.repository.tool_repository import ProviderConnectionRepository


class _FakeResult:
    """提供 SQLAlchemy 映射结果所需的最小接口。"""

    def mappings(self):
        """返回自身以支持 mappings().first() 调用链。"""
        return self

    def first(self):
        """模拟未查询到连接。"""
        return None


class _FakeSession:
    """记录仓储实际提交的 SQL 与绑定参数。"""

    def __init__(self) -> None:
        self.statement = None
        self.parameters = None

    async def execute(self, statement, parameters):
        """记录查询并返回空结果。"""
        self.statement = statement
        self.parameters = parameters
        return _FakeResult()


@pytest.mark.asyncio
async def test_find_active_connection_adds_typed_connection_filter_only_when_requested() -> None:
    """测试指定连接时以显式 UUID 条件查询，避免 asyncpg 推断可选 UUID 参数失败。"""
    session = _FakeSession()
    connection_id = UUID("10000000-0000-4000-8000-000000000021")

    result = await ProviderConnectionRepository().find_active_connection(
        session,
        tenant_id=UUID("2f744d3e-2a54-4e4f-aec3-bc9f0e2b6966"),
        user_id=UUID("b0071771-d192-4c32-a4e0-7b214eec2be6"),
        providers=("gmail", "outlook_mail"),
        required_scope="mail.send",
        required_connection_id=connection_id,
    )

    assert result is None
    assert session.parameters["connection_id"] == connection_id
    assert session.parameters["scope_json"] == '["mail.send"]'
    assert "id = CAST(:connection_id AS uuid)" in str(session.statement)
    assert ":connection_id IS NULL" not in str(session.statement)


@pytest.mark.asyncio
async def test_find_active_connection_omits_optional_filters_when_not_requested() -> None:
    """测试未限定连接或权限时不传入无类型的空参数。"""
    session = _FakeSession()

    await ProviderConnectionRepository().find_active_connection(
        session,
        tenant_id=UUID("2f744d3e-2a54-4e4f-aec3-bc9f0e2b6966"),
        user_id=UUID("b0071771-d192-4c32-a4e0-7b214eec2be6"),
        providers=("gmail",),
        required_scope=None,
    )

    assert "connection_id" not in session.parameters
    assert "scope_json" not in session.parameters
    assert "scopes @>" not in str(session.statement)
