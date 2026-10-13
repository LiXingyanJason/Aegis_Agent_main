"""最近邮件卡片引用来源：空或过期卡片不得回退为收件箱第一封。"""
from types import SimpleNamespace
from uuid import UUID

import pytest

from aegis_agent_worker.repository.decision_repository import DecisionRepository


class Result:
    def __init__(self, row=None):
        self.row = row
    def mappings(self):
        return self
    def first(self):
        return self.row
    def all(self):
        raise AssertionError("已有最近卡片时禁止回退为收件箱排序")


class Session:
    def __init__(self, artifact, rows=()):
        self.results = iter([artifact, *rows])
        self.statements = []
    async def execute(self, statement, args):
        self.statements.append(str(statement))
        return Result(next(self.results))


RUN = SimpleNamespace(tenant_id=UUID(int=1), user_id=UUID(int=2), conversation_id=UUID(int=3))


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact", [
    {"expired": True, "payload": {"items": [{"email_id": str(UUID(int=10))}]}},
    {"expired": False, "payload": {"items": []}},
])
async def test_expired_or_empty_latest_card_never_falls_back_to_inbox(artifact):
    """实际Repository读取到空／过期最近卡片，返回空引用，不再查询收件箱。"""
    session = Session(artifact)
    assert await DecisionRepository().list_references(session, RUN) == []
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_reference_positions_follow_card_and_stale_source_does_not_shift_numbers():
    """卡片第一封版本变化时不把第二封移成第一封，保留原position并校验版本。"""
    artifact = {"expired": False, "payload": {"query_id": "query1", "items": [
        {"email_id": str(UUID(int=10)), "source_version": "v1", "position": 1},
        {"email_id": str(UUID(int=11)), "source_version": "v1", "position": 2},
    ]}}
    session = Session(artifact, [
        {"id": UUID(int=10), "subject": "第一封", "source_version": "v2"},
        {"id": UUID(int=11), "subject": "第二封", "source_version": "v1"},
    ])
    refs = await DecisionRepository().list_references(session, RUN)
    assert len(refs) == 1 and refs[0]["position"] == 2 and refs[0]["query_id"] == "query1"
