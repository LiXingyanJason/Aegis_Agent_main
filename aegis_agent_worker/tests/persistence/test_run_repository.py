"""Agent 任务运行仓储的关键数据库状态测试。"""

from typing import Any
from uuid import UUID

import pytest

from aegis_agent_worker.persistence.run_repository import AgentRunRepository


class _Mappings:
    """模拟仓储方法使用的 first()、one() 查询结果。"""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def first(self) -> dict[str, Any] | None:
        """返回可选的一条记录。"""
        return self._row

    def one(self) -> dict[str, Any]:
        """返回必须存在的一条记录。"""
        assert self._row is not None
        return self._row


class _Result:
    """模拟 SQLAlchemy execute 返回对象。"""

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self._mappings = _Mappings(row)

    def mappings(self) -> _Mappings:
        """返回映射结果读取器。"""
        return self._mappings


class _FakeSession:
    """按顺序提供查询结果并记录 SQL 文本。"""

    def __init__(self) -> None:
        self._results = iter([_Result({"id": CONVERSATION_ID}), _Result({"next_sequence_no": 2})])
        self.statements: list[str] = []

    async def execute(self, statement: object, _parameters: dict[str, object]) -> _Result:
        """记录 SQL；前两次返回锁定会话和消息序号所需的记录。"""
        self.statements.append(str(statement))
        try:
            return next(self._results)
        except StopIteration:
            return _Result()


CONVERSATION_ID = UUID("8bfc8088-26d5-4c19-8876-7caa364b59c0")
RUN_ID = UUID("2dde4c59-6667-4af5-b12d-0f0c5cbcc2a6")
TENANT_ID = UUID("2f744d3e-2a54-4d4f-aec3-bc9f0e2b6966")
USER_ID = UUID("b0071771-d192-4c32-a4e0-7b214eec2be6")
STEP_ID = UUID("734977f1-0a41-4b4f-b9bd-0e960de6f9d2")


@pytest.mark.asyncio
async def test_complete_run_marks_run_step_as_succeeded() -> None:
    """测试 run_steps 使用数据库约束允许的 succeeded 完成状态，而非 agent_runs 的 completed。"""
    session = _FakeSession()
    repository = AgentRunRepository()

    await repository.complete_run_with_assistant_message(
        session,
        run_id=RUN_ID,
        conversation_id=CONVERSATION_ID,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        assistant_content="模型回复",
        step_id=STEP_ID,
    )

    assert "status = 'succeeded'" in session.statements[3]
    assert "status = 'completed'" in session.statements[4]
