"""历史会话读取接口的测试。"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.controller.conversation_controller import router
from app.repository.user_repository import LocalUser
from app.security.dependencies import CurrentUser, get_current_user, get_tenant_session


class _Mappings:
    """模拟 SQLAlchemy 查询结果的 mappings() 接口。"""

    def __init__(self, rows: list[dict[str, Any]], first_row: dict[str, Any] | None = None) -> None:
        self._rows = rows
        self._first_row = first_row

    def all(self) -> list[dict[str, Any]]:
        """返回全部模拟查询记录。"""
        return self._rows

    def first(self) -> dict[str, Any] | None:
        """返回第一条模拟查询记录。"""
        return self._first_row


class _Result:
    """模拟一个只使用 mappings() 的 SQLAlchemy 执行结果。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None, first_row: dict[str, Any] | None = None) -> None:
        self._mappings = _Mappings(rows or [], first_row)

    def mappings(self) -> _Mappings:
        """返回可按字典字段读取的模拟结果。"""
        return self._mappings


class _ScriptedSession:
    """按既定顺序返回查询结果，并记录每次传入的租户、用户参数。"""

    def __init__(self, results: list[_Result]) -> None:
        self._results: Iterator[_Result] = iter(results)
        self.calls: list[dict[str, object]] = []

    async def execute(self, _statement: object, parameters: dict[str, object]) -> _Result:
        """模拟仓储层查询。"""
        self.calls.append(parameters)
        return next(self._results)


LOCAL_USER = LocalUser(
    id=UUID("b0071771-d192-4c32-a4e0-7b214eec2be6"),
    tenant_id=UUID("2f744d3e-2a54-4d4f-aec3-bc9f0e2b6966"),
    email="jason@example.com",
    display_name="Jason",
)
CONVERSATION_ID = UUID("8bfc8088-26d5-4c19-8876-7caa364b59c0")
RUN_ID = UUID("2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6")


def _create_test_app(fake_session: _ScriptedSession) -> FastAPI:
    """创建以伪造身份和数据库会话运行的接口测试应用。"""
    app = FastAPI()
    app.include_router(router)

    async def override_current_user() -> CurrentUser:
        """模拟已经通过 JWT 验证且映射成功的本地用户。"""
        return CurrentUser(user=LOCAL_USER)

    async def override_tenant_session():
        """模拟已设置租户 RLS 上下文的数据库会话。"""
        yield fake_session

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_tenant_session] = override_tenant_session
    return app


def test_list_conversations_returns_only_current_users_summaries() -> None:
    """测试历史列表按当前用户、当前租户查询，并返回页面需要的会话摘要。"""
    created_at = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
    fake_session = _ScriptedSession(
        [
            _Result(
                rows=[
                    {
                        "id": CONVERSATION_ID,
                        "title": "明日项目同步",
                        "status": "active",
                        "last_message_at": created_at,
                        "latest_message_preview": "已生成会议安排预览。",
                        "created_at": created_at,
                    }
                ]
            )
        ]
    )
    app = _create_test_app(fake_session)

    with TestClient(app) as client:
        response = client.get("/conversations", headers={"Authorization": "Bearer verified-test-token"})

    assert response.status_code == 200
    assert response.json()["data"][0]["conversation_id"] == str(CONVERSATION_ID)
    assert response.json()["data"][0]["latest_message_preview"] == "已生成会议安排预览。"
    assert fake_session.calls == [{"tenant_id": LOCAL_USER.tenant_id, "user_id": LOCAL_USER.id}]


def test_get_conversation_detail_restores_messages_and_execution_data() -> None:
    """测试恢复单个会话时同时返回消息、任务步骤、工具预览和逐项确认。"""
    created_at = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
    message_id = UUID("0c07e3b2-dc2c-47bd-95fb-5ce39db187d9")
    step_id = UUID("734977f1-0a41-4b4f-b9bd-0e960de6f9d2")
    tool_call_id = UUID("eb93150c-c1fb-4c57-957b-99791bcc5b2a")
    approval_id = UUID("33af4c1d-3b45-4478-8957-51faa0f2c47d")
    fake_session = _ScriptedSession(
        [
            _Result(
                first_row={
                    "id": CONVERSATION_ID,
                    "title": "明日项目同步",
                    "status": "active",
                    "last_message_at": created_at,
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            ),
            _Result(
                rows=[
                    {
                        "id": message_id,
                        "role": "assistant",
                        "display_content": "已生成会议安排预览。",
                        "sequence_no": 2,
                        "is_final": True,
                        "run_id": RUN_ID,
                        "tool_call_id": tool_call_id,
                        "created_at": created_at,
                    }
                ]
            ),
            _Result(
                rows=[
                    {
                        "id": RUN_ID,
                        "status": "waiting_approval",
                        "current_stage": "等待确认",
                        "result_summary": None,
                        "error_code": None,
                        "error_message": None,
                        "started_at": created_at,
                        "finished_at": None,
                        "created_at": created_at,
                    }
                ]
            ),
            _Result(
                rows=[
                    {
                        "id": step_id,
                        "sequence_no": 1,
                        "step_key": "calendar_lookup",
                        "label": "查询双方可用时间",
                        "status": "completed",
                        "detail": "已找到 2 个候选时间。",
                        "started_at": created_at,
                        "finished_at": created_at,
                    }
                ]
            ),
            _Result(
                rows=[
                    {
                        "id": tool_call_id,
                        "tool_name": "calendar.find_availability",
                        "risk_level": "low",
                        "input_summary": "查询王敏与 Jason 的空闲时间",
                        "output_summary": "找到 2 个候选时间",
                        "status": "succeeded",
                        "error_code": None,
                        "error_message": None,
                        "duration_ms": 120,
                        "created_at": created_at,
                    }
                ]
            ),
            _Result(
                rows=[
                    {
                        "id": approval_id,
                        "action": "send_email",
                        "risk_level": "high",
                        "resource_type": "mail_draft",
                        "resource_id": "draft-001",
                        "title": "发送会议确认邮件",
                        "preview_snapshot": {"subject": "项目同步确认"},
                        "draft_version": 1,
                        "status": "pending",
                        "expires_at": None,
                        "created_at": created_at,
                    }
                ]
            ),
        ]
    )
    app = _create_test_app(fake_session)

    with TestClient(app) as client:
        response = client.get(
            f"/conversations/{CONVERSATION_ID}",
            headers={"Authorization": "Bearer verified-test-token"},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["messages"][0]["message_id"] == str(message_id)
    assert data["runs"][0]["steps"][0]["step_id"] == str(step_id)
    assert data["runs"][0]["tool_previews"][0]["tool_call_id"] == str(tool_call_id)
    assert data["runs"][0]["approval_items"][0]["approval_item_id"] == str(approval_id)
    assert fake_session.calls[0] == {
        "conversation_id": CONVERSATION_ID,
        "tenant_id": LOCAL_USER.tenant_id,
        "user_id": LOCAL_USER.id,
    }


def test_get_conversation_detail_hides_non_owned_conversation() -> None:
    """测试会话不属于当前用户或不存在时，统一返回 404，不泄露归属信息。"""
    fake_session = _ScriptedSession([_Result(first_row=None)])
    app = _create_test_app(fake_session)

    with TestClient(app) as client:
        response = client.get(
            f"/conversations/{CONVERSATION_ID}",
            headers={"Authorization": "Bearer verified-test-token"},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "会话不存在或无权访问"
