"""待办计划接口的无数据库单元测试。"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.controller import todo_controller
from app.param.todo_update_param import TodoUpdateParam
from app.repository.user_repository import LocalUser
from app.security.dependencies import CurrentUser


USER = LocalUser(
    id=UUID("10000000-0000-4000-8000-000000000010"),
    tenant_id=UUID("10000000-0000-4000-8000-000000000001"),
    email="jason@example.com",
    display_name="Jason",
)
TODO_ID = UUID("80f13cd3-057d-4e04-9ca8-6b4c36c28190")
EMAIL_ID = UUID("a8d1baa4-1fc3-4cc9-8500-b43aebff034e")
EXTRACTION_ID = UUID("0e5f9408-d763-4b60-a77f-48849f4ee3be")


def _todo_item() -> dict:
    """返回与 Repository 查询列一致的一条待办记录。"""
    now = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    return {
        "id": TODO_ID,
        "email_id": EMAIL_ID,
        "extraction_id": EXTRACTION_ID,
        "content": "确认续约报价",
        "due_at": None,
        "status": "draft",
        "completed_at": None,
        "source_subject": "2026 年服务续约确认",
        "source_sender": "李莉",
        "created_at": now,
        "updated_at": now,
    }


class _FakeTodoService:
    """替代数据库服务，验证 Controller 的响应转换与参数传递。"""

    def __init__(self) -> None:
        self.status_filter = "not-called"
        self.changes = None

    async def list_todos(self, _session, _user, status_filter):
        self.status_filter = status_filter
        return [_todo_item()]

    async def get_todo(self, _session, _user, todo_id):
        return _todo_item() if todo_id == TODO_ID else None

    async def update_todo(self, _session, _user, todo_id, param):
        self.changes = param.model_dump(exclude_unset=True)
        if todo_id != TODO_ID:
            return None
        item = _todo_item()
        item.update(self.changes)
        return item


def test_list_todos_uses_optional_status_filter(monkeypatch) -> None:
    """测试待办页面可传状态筛选，并返回统一 data 包装。"""
    fake = _FakeTodoService()
    monkeypatch.setattr(todo_controller, "_todo_service", fake)

    response = asyncio.run(
        todo_controller.list_todos(
            status_filter="draft", current_user=CurrentUser(user=USER), session=object()
        )
    )

    assert fake.status_filter == "draft"
    assert response["data"][0].todo_id == TODO_ID


def test_update_todo_allows_content_and_completion(monkeypatch) -> None:
    """测试页面编辑内容并标记完成时，Controller 保留实际提交字段。"""
    fake = _FakeTodoService()
    monkeypatch.setattr(todo_controller, "_todo_service", fake)
    param = TodoUpdateParam(content="确认最终续约报价", status="completed")

    response = asyncio.run(
        todo_controller.update_todo(TODO_ID, param, CurrentUser(user=USER), session=object())
    )

    assert fake.changes == {"content": "确认最终续约报价", "status": "completed"}
    assert response["data"].content == "确认最终续约报价"


def test_update_param_rejects_empty_patch() -> None:
    """测试空 PATCH 在进入数据库前被参数校验拒绝。"""
    with pytest.raises(ValueError, match="至少提供一个待修改字段"):
        TodoUpdateParam()
