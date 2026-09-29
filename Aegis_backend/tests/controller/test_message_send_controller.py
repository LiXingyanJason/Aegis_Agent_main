"""发送会话消息接口的测试。"""

from collections.abc import Iterator
from hashlib import sha256
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.controller.conversation_controller import router
from app.repository.user_repository import LocalUser
from app.security.dependencies import CurrentUser, get_current_user, get_tenant_session
from app.service.run_dispatch_service import RunDispatchService, get_run_dispatcher_from_request


class _Mappings:
    """模拟消息接口使用的 SQLAlchemy mappings 查询结果。"""

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self._row = row

    def first(self) -> dict[str, Any] | None:
        """返回可选的一条查询结果。"""
        return self._row

    def one(self) -> dict[str, Any]:
        """返回必须存在的一条 INSERT 或聚合查询结果。"""
        assert self._row is not None
        return self._row


class _Result:
    """模拟 SQLAlchemy execute 的返回值。"""

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self._mappings = _Mappings(row)

    def mappings(self) -> _Mappings:
        """提供字典形式的查询结果。"""
        return self._mappings


class _ScriptedSession:
    """按预设顺序返回数据库结果，并记录各次 SQL 参数。"""

    def __init__(self, results: list[_Result]) -> None:
        self._results: Iterator[_Result] = iter(results)
        self.calls: list[dict[str, object]] = []

    async def execute(self, _statement: object, parameters: dict[str, object]) -> _Result:
        """模拟一次数据库执行。"""
        self.calls.append(parameters)
        return next(self._results)


class _FakeRunDispatcher:
    """替代真实 Redis 投递，记录 Controller 是否登记任务入队。"""

    def __init__(self) -> None:
        self.run_ids: list[UUID] = []

    def enqueue_after_commit(self, run_id: UUID) -> None:
        """记录应在事务提交后投递的任务。"""
        self.run_ids.append(run_id)


LOCAL_USER = LocalUser(
    id=UUID("b0071771-d192-4c32-a4e0-7b214eec2be6"),
    tenant_id=UUID("2f744d3e-2a54-4d4f-aec3-bc9f0e2b6966"),
    email="jason@example.com",
    display_name="Jason",
)
CONVERSATION_ID = UUID("8bfc8088-26d5-4c19-8876-7caa364b59c0")
MESSAGE_ID = UUID("0c07e3b2-dc2c-47bd-95fb-5ce39db187d9")
RUN_ID = UUID("2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6")


def _create_test_app(fake_session: _ScriptedSession) -> tuple[FastAPI, _FakeRunDispatcher]:
    """创建使用伪造已认证用户和租户会话的测试应用。"""
    app = FastAPI()
    app.include_router(router)

    async def override_current_user() -> CurrentUser:
        """模拟已经完成 OIDC 身份映射的本地用户。"""
        return CurrentUser(user=LOCAL_USER)

    async def override_tenant_session():
        """模拟包含 RLS 上下文的事务会话。"""
        yield fake_session

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_tenant_session] = override_tenant_session
    fake_dispatcher = _FakeRunDispatcher()

    def override_dispatcher() -> RunDispatchService:
        """避免接口测试依赖真实 Redis 与数据库提交钩子。"""
        return fake_dispatcher  # type: ignore[return-value]

    app.dependency_overrides[get_run_dispatcher_from_request] = override_dispatcher
    return app, fake_dispatcher


def test_send_message_persists_message_creates_queued_run_and_updates_conversation() -> None:
    """测试发送消息会在同一事务中写入消息、任务运行和会话最近活动时间。"""
    fake_session = _ScriptedSession(
        [
            _Result(
                {"id": CONVERSATION_ID}  # 当前用户拥有该会话，并为本次写入加锁。
            ),
            _Result(None),  # 未发现相同 client_message_id 的历史消息。
            _Result({"next_sequence_no": 1}),
            _Result({"id": MESSAGE_ID}),
            _Result({"id": RUN_ID, "status": "queued"}),
            _Result(),
            _Result(),
        ]
    )
    app, fake_dispatcher = _create_test_app(fake_session)
    content = "  帮我安排明天的项目同步。  "

    with TestClient(app) as client:
        response = client.post(
            f"/conversations/{CONVERSATION_ID}/messages",
            headers={"Authorization": "Bearer verified-test-token"},
            json={"content": content, "client_message_id": "web-message-001"},
        )

    assert response.status_code == 202
    assert response.json() == {
        "data": {
            "message_id": str(MESSAGE_ID),
            "run_id": str(RUN_ID),
            "status": "queued",
            "idempotent_replay": False,
        }
    }
    assert fake_session.calls[3]["content"] == "帮我安排明天的项目同步。"
    assert fake_session.calls[3]["content_hash"] == sha256(
        "帮我安排明天的项目同步。".encode("utf-8")
    ).hexdigest()
    assert fake_session.calls[4]["input_message_id"] == MESSAGE_ID
    assert fake_session.calls[6] == {
        "conversation_id": CONVERSATION_ID,
        "tenant_id": LOCAL_USER.tenant_id,
        "user_id": LOCAL_USER.id,
    }
    assert fake_dispatcher.run_ids == [RUN_ID]


def test_send_message_returns_existing_run_when_client_message_id_replayed() -> None:
    """测试网络重试复用相同 client_message_id 时，不重复创建消息或任务。"""
    fake_session = _ScriptedSession(
        [
            _Result({"id": CONVERSATION_ID}),
            _Result({"message_id": MESSAGE_ID, "run_id": RUN_ID, "status": "queued"}),
        ]
    )
    app, fake_dispatcher = _create_test_app(fake_session)

    with TestClient(app) as client:
        response = client.post(
            f"/conversations/{CONVERSATION_ID}/messages",
            headers={"Authorization": "Bearer verified-test-token"},
            json={"content": "帮我安排明天的项目同步。", "client_message_id": "web-message-001"},
        )

    assert response.status_code == 202
    assert response.json()["data"]["idempotent_replay"] is True
    assert len(fake_session.calls) == 2
    assert fake_dispatcher.run_ids == [RUN_ID]


def test_send_message_hides_non_owned_conversation() -> None:
    """测试向不存在或非本人会话发送消息时统一返回 404。"""
    fake_session = _ScriptedSession([_Result(None)])
    app, fake_dispatcher = _create_test_app(fake_session)

    with TestClient(app) as client:
        response = client.post(
            f"/conversations/{CONVERSATION_ID}/messages",
            headers={"Authorization": "Bearer verified-test-token"},
            json={"content": "安排会议", "client_message_id": "web-message-001"},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "会话不存在或无权访问"
    assert fake_dispatcher.run_ids == []


def test_send_message_rejects_blank_content_before_database_access() -> None:
    """测试纯空白任务文本被参数校验拒绝，避免创建无效消息和任务。"""
    fake_session = _ScriptedSession([])
    app, fake_dispatcher = _create_test_app(fake_session)

    with TestClient(app) as client:
        response = client.post(
            f"/conversations/{CONVERSATION_ID}/messages",
            headers={"Authorization": "Bearer verified-test-token"},
            json={"content": "   ", "client_message_id": "web-message-001"},
        )

    assert response.status_code == 422
    assert fake_session.calls == []
    assert fake_dispatcher.run_ids == []
