"""创建会话受保护接口的测试。"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.controller.conversation_controller import router
from app.repository.user_repository import LocalUser
from app.security.dependencies import CurrentUser, get_current_user, get_tenant_session


class _FakeMappings:
    """模拟 SQLAlchemy 查询结果的 mappings().one() 调用。"""

    def one(self) -> dict[str, object]:
        """返回一条模拟的 INSERT RETURNING 记录。"""
        return {
            "id": UUID("8bfc8088-26d5-4c19-8876-7caa364b59c0"),
            "title": "明日项目同步",
            "status": "active",
            "created_at": datetime(2026, 9, 28, 8, 0, tzinfo=UTC),
        }


class _FakeResult:
    """模拟 SQLAlchemy 执行结果。"""

    def mappings(self) -> _FakeMappings:
        """返回可读取单条映射记录的对象。"""
        return _FakeMappings()


class _FakeSession:
    """记录 Controller 最终提交给仓储层的 SQL 参数。"""

    def __init__(self) -> None:
        self.parameters: dict[str, object] | None = None

    async def execute(self, _statement: object, parameters: dict[str, object]) -> _FakeResult:
        """模拟 INSERT 执行并保留参数以验证用户归属。"""
        self.parameters = parameters
        return _FakeResult()


def _create_test_app() -> FastAPI:
    """创建不启动真实数据库、Redis 与 Keycloak 的接口测试应用。"""
    app = FastAPI()
    app.include_router(router)
    return app


def test_create_conversation_requires_bearer_token() -> None:
    """测试未携带 Bearer token 时，创建会话接口返回 401。"""
    app = _create_test_app()

    with TestClient(app) as client:
        response = client.post("/conversations", json={"title": "明日项目同步"})

    assert response.status_code == 401
    assert response.json()["detail"] == "缺少 Bearer access token"


def test_create_conversation_uses_authenticated_user_and_returns_data() -> None:
    """测试已认证身份只能以自身租户、用户标识创建会话并获得统一响应。"""
    app = _create_test_app()
    fake_session = _FakeSession()
    local_user = LocalUser(
        id=UUID("b0071771-d192-4c32-a4e0-7b214eec2be6"),
        tenant_id=UUID("2f744d3e-2a54-4d4f-aec3-bc9f0e2b6966"),
        email="jason@example.com",
        display_name="Jason",
    )

    async def override_current_user() -> CurrentUser:
        """模拟已经过 JWT 验证及本地映射的用户。"""
        return CurrentUser(user=local_user)

    async def override_tenant_session():
        """模拟已经建立 RLS 上下文的数据库会话。"""
        yield fake_session

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_tenant_session] = override_tenant_session

    with TestClient(app) as client:
        response = client.post(
            "/conversations",
            headers={"Authorization": "Bearer verified-test-token"},
            json={"title": "  明日项目同步  "},
        )

    assert response.status_code == 201
    assert response.json() == {
        "data": {
            "conversation_id": "8bfc8088-26d5-4c19-8876-7caa364b59c0",
            "title": "明日项目同步",
            "status": "active",
            "created_at": "2026-09-28T08:00:00Z",
        }
    }
    assert fake_session.parameters == {
        "tenant_id": local_user.tenant_id,
        "user_id": local_user.id,
        "title": "明日项目同步",
    }
