"""信息收集多次读取与恢复核验的步骤键回归。"""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from aegis_agent_worker.agent.contracts.react import ReActDecision
from aegis_agent_worker.service.mail.mail_collection_service import MailCollectionService


@pytest.mark.asyncio
async def test_repeated_collection_reads_create_distinct_auditable_steps():
    """同一run反复调用同一工具时，步骤键不重复，成功记录分别关联各自步骤。"""
    class Session:
        @asynccontextmanager
        async def begin(self):
            yield
        async def execute(self, *args):
            pass

    class Database:
        @asynccontextmanager
        async def session(self):
            yield Session()

    class Runs:
        keys = set()
        steps = []
        finished = []
        async def create_running_tool_step(self, session, **kwargs):
            assert kwargs["step_key"] not in self.keys, "数据库唯一键冲突"
            self.keys.add(kwargs["step_key"])
            step = uuid4()
            self.steps.append(step)
            return step
        async def finish_step(self, session, **kwargs):
            self.finished.append(kwargs["step_id"])
        async def append_event(self, session, **kwargs):
            return kwargs

    class Calls:
        steps = []
        async def create_running(self, session, **kwargs):
            self.steps.append(kwargs["step_id"])
            return uuid4()
        async def succeed(self, session, **kwargs):
            pass

    class Tools:
        async def prepare(self, session, **kwargs):
            return SimpleNamespace(
                registered=SimpleNamespace(definition=SimpleNamespace(
                    name="mail.messages.list", risk_level="read", version="1", mcp_server="email")),
                context=SimpleNamespace(connection_id=UUID(int=4)),
            )
        async def invoke(self, prepared):
            return SimpleNamespace(response_payload={"messages": []})

    runs, calls = Runs(), Calls()
    service = MailCollectionService(Database(), runs, Tools())
    service._calls = calls
    run = SimpleNamespace(id=UUID(int=1), tenant_id=UUID(int=2), user_id=UUID(int=3))
    decision = ReActDecision(action="call_tool", tool_name="mail.messages.list")
    for _ in range(3):
        assert await service.invoke(run, decision, UUID(int=4)) == {"messages": []}
    assert len(runs.keys) == 3
    assert runs.steps == runs.finished == calls.steps
