"""站内卡片工具的会话隔离、最小输出、过期与最终版本核验。"""
from contextlib import asynccontextmanager
from datetime import datetime, UTC, timedelta
from uuid import UUID
import json

import pytest

from aegis_agent_worker.agent.contracts.context import CardsGetArgs, MainContextReadRequest, CardReference
from aegis_agent_worker.config.settings import Settings
from aegis_agent_worker.harness.policy import TaskExecutionPolicy
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.service.runtime.card_context_service import CardContextService
from aegis_agent_worker.tool.bootstrap import create_default_tool_gateway
from aegis_agent_worker.tool.contracts import ToolContext, ToolError, ToolInvocation


RUN = ClaimedRun(id=UUID(int=1), tenant_id=UUID(int=2), user_id=UUID(int=3), conversation_id=UUID(int=4))
CONTEXT = ToolContext(RUN.tenant_id, RUN.user_id, RUN.id)
CARD_ID, QUERY_ID = UUID(int=5), UUID(int=6)


class Database:
    @asynccontextmanager
    async def session(self):
        class Session:
            @asynccontextmanager
            async def begin(self): yield
            async def execute(self, *args): pass
        yield Session()


def card(expired=False):
    now = datetime.now(UTC)
    return {"id": CARD_ID, "run_id": QUERY_ID, "artifact_type": "mail_candidates", "expired": expired,
            "created_at": now, "expires_at": now + timedelta(days=1), "item_count": 2,
            "payload": {"query_id": str(QUERY_ID), "items": [
                {"position": 1, "email_id": str(UUID(int=9)), "source_version": "v1", "subject": "续约",
                 "body": "PRIVATE BODY", "access_token": "PRIVATE SECRET"},
                {"position": 2, "email_id": str(UUID(int=10)), "source_version": "v1", "subject": "评审"},
            ]}}


class Repository:
    def __init__(self):
        self.card, self.email_version, self.conversation = card(), "v1", RUN.conversation_id
        self.email_reads = 0
    async def conversation_for_run(self, session, context): return self.conversation
    async def get_card(self, session, context, conversation, args):
        assert conversation == RUN.conversation_id
        return self.card if args.card_id in {None, CARD_ID} else None
    async def list_cards(self, session, context, conversation, type, limit): return [self.card]
    async def current_email(self, session, context, id):
        self.email_reads += 1
        return {"id": id, "source_version": self.email_version if id == UUID(int=9) else "v1"}


@pytest.mark.asyncio
async def test_card_output_is_safe_and_stale_first_position_is_not_replaced():
    """输出裁剪正文凭据，版本失效的第一位置仍保留，第二位置不重编号。"""
    repository = Repository()
    service = CardContextService(Database(), repository)
    result = await service.read(CONTEXT, "conversation.cards.get", {"selector": "latest"})
    assert "PRIVATE" not in json.dumps(result) and result["items"][0]["status"] == "valid"
    repository.email_version = "v2"
    result = await service.read(CONTEXT, "conversation.cards.get", {"card_id": str(CARD_ID)})
    assert result["items"][0] == {"position": 1, "status": "stale"}
    assert result["items"][1]["position"] == 2


@pytest.mark.asyncio
async def test_expired_card_and_index_do_not_expose_executable_items():
    """过期get不查邮件或返回执行引用，list只返回索引。"""
    repository = Repository()
    repository.card = card(True)
    service = CardContextService(Database(), repository)
    result = await service.read(CONTEXT, "conversation.cards.get", {"selector": "latest"})
    assert result["status"] == "expired" and result["items"] == [] and not repository.email_reads
    result = await service.read(CONTEXT, "conversation.cards.list", {})
    assert "payload" not in str(result) and "items" not in result["cards"][0]


@pytest.mark.asyncio
async def test_not_owned_card_or_non_conversation_run_has_same_safe_error():
    """外部卡片ID和非会话run不能读取上下文，统一错误不泄露存在性。"""
    repository = Repository()
    service = CardContextService(Database(), repository)
    with pytest.raises(ToolError) as error:
        await service.read(CONTEXT, "conversation.cards.get", {"card_id": str(UUID(int=99))})
    assert error.value.code == "CARD_NOT_FOUND"
    repository.conversation = None
    with pytest.raises(ToolError) as error:
        await service.read(CONTEXT, "conversation.cards.list", {})
    assert error.value.code == "CARD_NOT_FOUND"


@pytest.mark.asyncio
async def test_final_resolution_rechecks_card_id_query_position_and_source_version():
    """只有本轮成功get的卡片可用；推理期间版本变化必须再次拒绝。"""
    repository = Repository()
    service = CardContextService(Database(), repository)
    ref = CardReference(card_id=CARD_ID, query_id=QUERY_ID, position=1)
    assert await service.resolve(RUN, ref, []) is None
    result = await service.read(CONTEXT, "conversation.cards.get", {"selector": "latest"})
    assert (await service.resolve(RUN, ref, [result]))["id"] == str(UUID(int=9))
    assert await service.resolve(RUN, ref.model_copy(update={"query_id": UUID(int=99)}), [result]) is None
    assert await service.resolve(RUN, ref.model_copy(update={"position": 3}), [result]) is None
    repository.email_version = "v2"
    assert await service.resolve(RUN, ref, [result]) is None


@pytest.mark.parametrize("arguments", [
    {}, {"card_id": str(CARD_ID), "selector": "latest"}, {"selector": "first"},
    {"selector": "latest", "user_id": "attacker"}, {"selector": "latest", "conversation_id": str(UUID(int=99))},
])
def test_get_schema_rejects_missing_or_forged_identity(arguments):
    """模型不能传身份或SQL，也不能省略选择器或同时指定多个定位方式。"""
    with pytest.raises(ValueError): CardsGetArgs.model_validate(arguments)


def test_context_requests_cannot_call_business_tools_or_bypass_limit():
    """主Agent上下文能力白名单独立于领域能力，只接受卡片只读工具。"""
    with pytest.raises(ValueError):
        MainContextReadRequest(action="read_context", tool_name="mail.messages.send", arguments={})
    with pytest.raises(ValueError):
        MainContextReadRequest(action="read_context", tool_name="conversation.cards.list", arguments={"limit": 100})
    assert "conversation.cards.get" in TaskExecutionPolicy().tools("conversation")
    assert "conversation.cards.get" not in TaskExecutionPolicy().tools("mail_reply_draft")


@pytest.mark.asyncio
async def test_default_gateway_registers_internal_tool_without_external_connection():
    """真实默认Gateway注册内部工具，prepare不需要日历或邮箱连接。"""
    settings = Settings(_env_file=None, database_url="postgresql://x:y@localhost/db", redis_url="redis://localhost",
        model_provider="deepseek", model_api_base="https://model.example.com", model_api_key="x", model_default_name="test")
    gateway = create_default_tool_gateway(settings, card_context=CardContextService(Database(), Repository()))
    prepared = await gateway.prepare(None, tenant_id=RUN.tenant_id, user_id=RUN.user_id, run_id=RUN.id,
        invocation=ToolInvocation("conversation.cards.get", {"selector": "latest"}))
    assert prepared.context.connection_id is None
    result = await gateway.invoke(prepared)
    assert result.response_payload["card_id"] == str(CARD_ID)
    assert "subject" not in str(result.output_summary)


@pytest.mark.asyncio
async def test_real_repository_queries_bind_run_and_card_ownership():
    """实际SQL显式限制run、用户、租户与会话，即使数据库角色可绕过RLS也不能跨会话读卡片。"""
    from aegis_agent_worker.repository.conversation_card_repository import ConversationCardRepository
    class Session:
        def __init__(self): self.calls = []
        async def execute(self, sql, args):
            self.calls.append((str(sql), args))
            class Result:
                def mappings(self): return self
                def first(self): return {"conversation_id": RUN.conversation_id}
                def all(self): return []
            return Result()
    repository, session = ConversationCardRepository(), Session()
    await repository.conversation_for_run(session, CONTEXT)
    await repository.list_cards(session, CONTEXT, RUN.conversation_id, "mail_candidates", 3)
    await repository.get_card(session, CONTEXT, RUN.conversation_id, CardsGetArgs(card_id=CARD_ID))
    for sql, args in session.calls:
        assert "tenant_id=:tenant" in sql and "user_id=:user" in sql
        assert args["tenant"] == RUN.tenant_id and args["user"] == RUN.user_id
    assert "run_type='conversation'" in session.calls[0][0]
    assert all("conversation_id=:conversation" in sql for sql, _ in session.calls[1:])


@pytest.mark.asyncio
async def test_card_read_audits_unique_steps_without_saving_titles():
    """真实上下文Service为每次调用建立不同步骤，审计只保存来源摘要，不重复保存卡片正文。"""
    from aegis_agent_worker.service.runtime.main_context_tool_service import MainContextToolService
    from types import SimpleNamespace
    from aegis_agent_worker.tool.contracts import ToolResult
    from uuid import uuid4
    class Runs:
        def __init__(self): self.keys, self.events = [], []
        async def create_running_tool_step(self, session, **kwargs):
            self.keys.append(kwargs["step_key"]); return uuid4()
        async def finish_step(self, session, **kwargs): pass
        async def append_event(self, session, **kwargs):
            self.events.append(kwargs); return kwargs
    class Calls:
        def __init__(self): self.payloads = []
        async def create_running(self, session, **kwargs): return uuid4()
        async def succeed(self, session, **kwargs): self.payloads.append(kwargs["response_payload"])
    class Gateway:
        async def prepare(self, session, **kwargs):
            return SimpleNamespace(registered=SimpleNamespace(definition=SimpleNamespace(
                name="conversation.cards.get", version="1", mcp_server="aegis-internal")))
        async def invoke(self, prepared):
            return ToolResult({"items": [{"subject": "PRIVATE TITLE"}]}, {"card_id": str(CARD_ID), "status": "valid"})
    runs, calls = Runs(), Calls()
    service = MainContextToolService(Database(), runs, Gateway(), None, calls=calls)
    request = MainContextReadRequest(action="read_context", tool_name="conversation.cards.get", arguments={"selector": "latest"})
    await service.execute(RUN, request)
    await service.execute(RUN, request)
    assert len(set(runs.keys)) == 2 and "PRIVATE" not in str(calls.payloads)
    assert all(e["event_type"] == "tool_preview" for e in runs.events)
