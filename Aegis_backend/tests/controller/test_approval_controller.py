"""逐项确认接口测试。"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.controller.approval_controller import router
from app.repository.user_repository import LocalUser
from app.security.dependencies import CurrentUser, get_current_user, get_tenant_session
from app.service.run_dispatch_service import RunDispatchService, get_run_dispatcher_from_request


class _Mappings:
    """模拟审批仓储使用的 mappings().first()。"""

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self._row = row

    def first(self) -> dict[str, Any] | None:
        return self._row


class _Result:
    """模拟 SQLAlchemy 结果。"""

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self._mappings = _Mappings(row)

    def mappings(self) -> _Mappings:
        return self._mappings


class _Session:
    """按调用顺序提供审批查询、插入和更新的模拟结果。"""

    def __init__(self, results: list[_Result]) -> None:
        self._results: Iterator[_Result] = iter(results)
        self.calls: list[dict[str, object]] = []

    async def execute(self, _statement: object, parameters: dict[str, object]) -> _Result:
        self.calls.append(parameters)
        return next(self._results)


USER = LocalUser(
    id=UUID("b0071771-d192-4c32-a4e0-7b214eec2be6"),
    tenant_id=UUID("2f744d3e-2a54-4d4f-aec3-bc9f0e2b6966"),
    email="jason@example.com",
    display_name="Jason",
)
APPROVAL_ID = UUID("33af4c1d-3b45-4478-8957-51faa0f2c47d")
RUN_ID = UUID("2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6")


def _app(session: _Session) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def current_user() -> CurrentUser:
        return CurrentUser(user=USER)

    async def tenant_session():
        yield session

    class _Dispatcher:
        """记录确认接口是否登记了提交后恢复任务。"""

        def __init__(self) -> None:
            self.calls: list[tuple[UUID, str | None]] = []

        def enqueue_after_commit(self, run_id: UUID, *, resume_decision: str | None = None) -> None:
            self.calls.append((run_id, resume_decision))

    dispatcher = _Dispatcher()

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_tenant_session] = tenant_session
    app.dependency_overrides[get_run_dispatcher_from_request] = lambda: dispatcher
    app.state.test_dispatcher = dispatcher
    return app


def test_approve_pending_item_registers_resume_after_commit() -> None:
    """测试批准会登记提交后恢复 Worker 的任务，不在控制器内直接执行。"""
    session = _Session([
        _Result({"id": APPROVAL_ID, "run_id": RUN_ID, "status": "pending", "expired": False}),
        _Result(),
        _Result(),
    ])
    app = _app(session)
    with TestClient(app) as client:
        response = client.post(f"/approvals/{APPROVAL_ID}/decision", json={"decision": "approved"}, headers={"Authorization": "Bearer test"})

    assert response.status_code == 200
    assert response.json()["data"] == {
        "approval_item_id": str(APPROVAL_ID),
        "run_id": str(RUN_ID),
        "decision": "approved",
        "approval_status": "approved_executing",
        "execution_scheduled": True,
    }
    assert len(session.calls) == 3
    assert app.state.test_dispatcher.calls == [(RUN_ID, "approved")]
