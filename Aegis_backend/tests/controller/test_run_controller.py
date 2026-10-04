"""任务运行状态查询接口的测试。"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.controller.run_controller import router
from app.repository.user_repository import LocalUser
from app.security.dependencies import CurrentUser, get_current_user, get_tenant_session


class _Mappings:
    """模拟 SQLAlchemy mappings() 查询结果。"""

    def __init__(self, first_row: dict[str, Any] | None = None, rows: list[dict[str, Any]] | None = None) -> None:
        self._first_row = first_row
        self._rows = rows or []

    def first(self) -> dict[str, Any] | None:
        """返回可选的一条任务记录。"""
        return self._first_row

    def all(self) -> list[dict[str, Any]]:
        """返回全部任务步骤。"""
        return self._rows


class _Result:
    """模拟 SQLAlchemy execute 的结果包装。"""

    def __init__(self, first_row: dict[str, Any] | None = None, rows: list[dict[str, Any]] | None = None) -> None:
        self._mappings = _Mappings(first_row, rows)

    def mappings(self) -> _Mappings:
        """提供字典形式的查询结果。"""
        return self._mappings


class _ScriptedSession:
    """按调用顺序返回运行记录和步骤记录。"""

    def __init__(self, results: list[_Result]) -> None:
        self._results = iter(results)
        self.calls: list[dict[str, object]] = []

    async def execute(self, _statement: object, parameters: dict[str, object]) -> _Result:
        """记录租户范围查询参数并返回预设结果。"""
        self.calls.append(parameters)
        return next(self._results)


LOCAL_USER = LocalUser(
    id=UUID("b0071771-d192-4c32-a4e0-7b214eec2be6"),
    tenant_id=UUID("2f744d3e-2a54-4d4f-aec3-bc9f0e2b6966"),
    email="jason@example.com",
    display_name="Jason",
)
RUN_ID = UUID("2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6")
CONVERSATION_ID = UUID("8bfc8088-26d5-4c19-8876-7caa364b59c0")
STEP_ID = UUID("734977f1-0a41-4b4f-b9bd-0e960de6f9d2")


def _create_test_app(fake_session: _ScriptedSession) -> FastAPI:
    """创建使用伪造认证用户和租户事务的运行查询测试应用。"""
    app = FastAPI()
    app.include_router(router)

    async def override_current_user() -> CurrentUser:
        """模拟已完成 JWT 验证和本地映射的用户。"""
        return CurrentUser(user=LOCAL_USER)

    async def override_tenant_session():
        """模拟已设置 RLS 上下文的异步数据库会话。"""
        yield fake_session

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_tenant_session] = override_tenant_session
    return app


def test_get_run_detail_returns_status_model_and_steps() -> None:
    """测试任务查询返回当前状态、模型追溯信息和按序步骤。"""
    timestamp = datetime(2026, 10, 4, 8, 0, tzinfo=UTC)
    fake_session = _ScriptedSession(
        [
            _Result(
                first_row={
                    "id": RUN_ID,
                    "conversation_id": CONVERSATION_ID,
                    "status": "completed",
                    "current_stage": "已完成",
                    "model_provider": "deepseek",
                    "model_name": "deepseek-chat",
                    "result_summary": "已完成任务整理。",
                    "error_code": None,
                    "error_message": None,
                    "started_at": timestamp,
                    "finished_at": timestamp,
                    "created_at": timestamp,
                }
            ),
            _Result(
                rows=[
                    {
                        "id": STEP_ID,
                        "sequence_no": 1,
                        "step_key": "llm_generate",
                        "label": "正在生成回复",
                        "status": "succeeded",
                        "detail": "模型回复已保存",
                        "started_at": timestamp,
                        "finished_at": timestamp,
                    }
                ]
            ),
        ]
    )
    app = _create_test_app(fake_session)

    with TestClient(app) as client:
        response = client.get(f"/runs/{RUN_ID}", headers={"Authorization": "Bearer verified-test-token"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["run_id"] == str(RUN_ID)
    assert data["model_provider"] == "deepseek"
    assert data["steps"][0]["status"] == "succeeded"
    assert fake_session.calls[0] == {
        "run_id": RUN_ID,
        "tenant_id": LOCAL_USER.tenant_id,
        "user_id": LOCAL_USER.id,
    }


def test_get_run_detail_hides_non_owned_run() -> None:
    """测试任务不存在或不属于当前用户时统一返回 404。"""
    app = _create_test_app(_ScriptedSession([_Result(first_row=None)]))

    with TestClient(app) as client:
        response = client.get(f"/runs/{RUN_ID}", headers={"Authorization": "Bearer verified-test-token"})

    assert response.status_code == 404
    assert response.json()["detail"] == "任务不存在或无权访问"


def test_get_run_detail_requires_bearer_token() -> None:
    """测试缺少 Bearer token 时，任务状态查询被认证依赖拒绝。"""
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        response = client.get(f"/runs/{RUN_ID}")

    assert response.status_code == 401
    assert response.json()["detail"] == "缺少 Bearer access token"
