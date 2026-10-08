"""长期记忆 Controller 的无数据库单元测试。"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.controller import memory_controller
from app.param.memory_create_param import MemoryCreateParam
from app.repository.user_repository import LocalUser
from app.security.dependencies import CurrentUser


USER = LocalUser(
    id=UUID("10000000-0000-4000-8000-000000000010"),
    tenant_id=UUID("10000000-0000-4000-8000-000000000001"),
    email="jason@example.com",
    display_name="Jason",
)
MEMORY_ID = UUID("75e52ec3-12e9-4113-8e52-5466f96ab991")


def _memory_item(*, sensitive: bool = False) -> dict:
    """构造与记忆 Repository 返回列一致的测试数据。"""
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    return {
        "id": MEMORY_ID,
        "content": "周一上午不安排会议",
        "source": "user_input",
        "source_run_id": None,
        "is_sensitive": sensitive,
        "created_at": now,
        "updated_at": now,
    }


class _FakeMemoryService:
    """替代数据库服务，验证列表脱敏和 Controller 参数传递。"""

    async def list_memories(self, _session, _user, _query, _cursor, _limit):
        return [_memory_item(), _memory_item(sensitive=True)]

    async def create_memory(self, _session, _user, _param):
        return _memory_item()


def test_list_memories_masks_sensitive_content(monkeypatch) -> None:
    """测试敏感记忆在列表中不会返回原文。"""
    monkeypatch.setattr(memory_controller, "_memory_service", _FakeMemoryService())
    response = asyncio.run(
        memory_controller.list_memories(
            q=None, cursor=None, limit=50, current_user=CurrentUser(user=USER), session=object()
        )
    )
    assert response["data"].items[0].content_preview == "周一上午不安排会议"
    assert response["data"].items[1].content_preview == "敏感记忆（点击查看）"


def test_create_memory_rejects_high_risk_secret() -> None:
    """测试高危凭据会在进入业务层前被参数校验拒绝。"""
    with pytest.raises(ValueError, match="不允许保存"):
        MemoryCreateParam(content="API Key: sk-example")
