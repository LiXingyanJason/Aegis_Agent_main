"""主对话邮件查询从计划、Gateway、快照到 Artifact 的组合回归。"""

import json
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from aegis_agent_worker.agent.contracts.mail import MailQueryPlan, MailListMessage, build_mail_candidates
from aegis_agent_worker.agent.contracts.decision import MainDecision, ConversationCapability
from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
from aegis_agent_worker.agent.graph.task_graph import TaskGraph, TaskGraphDependencies
from aegis_agent_worker.agent.workflows.mail.query_workflow import MailQueryWorkflow
from aegis_agent_worker.service.mail.mail_query_service import MailQueryService
from aegis_agent_worker.service.runtime.conversation_artifact_service import ConversationArtifactService
from aegis_agent_worker.service.runtime.decision_context_service import DecisionContextService
from aegis_agent_worker.repository.run_repository import ClaimedRun
from aegis_agent_worker.tool.registry import ToolRegistry
from aegis_agent_worker.tool.gateway import ToolGateway
from aegis_agent_worker.tool.contracts import ToolDefinition, ToolResult
from aegis_agent_worker.repository.decision_repository import DecisionRepository

RUN = ClaimedRun(id=UUID(int=1), tenant_id=UUID(int=2), user_id=UUID(int=3), conversation_id=UUID(int=4))


def message(index=1, **extra):
    value = dict(provider_message_id=f"mail-{index}", provider_thread_id=None,
                 sender_name="客户", sender_email="customer@example.com", subject="本周续约",
                 received_at="2026-10-12T10:00:00+08:00", source_version="v1",
                 list_preview="确认续约报价", attachment_count=0)
    value.update(extra)
    return value


class Session:
    @asynccontextmanager
    async def begin(self):
        yield
    async def execute(self, *args):
        return None


class Database:
    @asynccontextmanager
    async def session(self):
        yield Session()


class Runs:
    def __init__(self):
        self.events = []
        self.steps = []
    async def create_running_tool_step(self, session, **kwargs):
        self.steps.append(kwargs)
        return UUID(int=10)
    async def finish_step(self, session, **kwargs):
        self.steps.append(kwargs)
    async def append_event(self, session, **kwargs):
        self.events.append(kwargs)
        return kwargs


class ToolCalls:
    def __init__(self):
        self.saved = None
        self.failed = None
    async def create_running(self, session, **kwargs):
        return UUID(int=11)
    async def bind_connection(self, session, **kwargs):
        assert kwargs["connection_id"] == UUID(int=12)
    async def succeed(self, session, **kwargs):
        self.saved = kwargs
    async def fail(self, session, **kwargs):
        self.failed = kwargs


class Connections:
    def __init__(self, allowed=True):
        self.allowed = allowed
    async def find_active_connection(self, session, **kwargs):
        assert kwargs["tenant_id"] == RUN.tenant_id and kwargs["user_id"] == RUN.user_id
        assert kwargs["required_scope"] == "mail.read"
        return SimpleNamespace(id=UUID(int=12)) if self.allowed else None


class Handler:
    def __init__(self, invalid=False, empty=False):
        self.calls = 0
        self.invalid, self.empty = invalid, empty
    async def invoke(self, context, arguments):
        self.calls += 1
        assert arguments["__tool_name"] == "mail.messages.list"
        data = {"source": "mock_mailbox", "messages": [] if self.empty else [
            message(body_text="SECRET BODY"),
            message(2, subject="产品介绍", list_preview="其他内容"),
        ]}
        if self.invalid:
            data["messages"] = [{}]
        return ToolResult(response_payload=data, output_summary={})


class Snapshots:
    def __init__(self):
        self.messages = []
    async def upsert(self, session, run, connection_id, messages):
        self.messages = messages
        return [dict(id=UUID(int=index + 30), subject=item.subject, sender_name=item.sender_name,
                     sender_email=item.sender_email, source_version=item.source_version,
                     received_at=item.received_at, extraction_status="not_generated")
                for index, item in enumerate(messages)]


class ArtifactRepository:
    def __init__(self):
        self.artifacts = []
    async def upsert_for_run(self, session, *, run, artifacts):
        self.artifacts = artifacts
        return [UUID(int=40)]


class Publisher:
    def publish_after_commit(self, event):
        """测试用通知发布器，不连接 Redis。"""
        pass


class Client:
    provider_name = "test"
    model_name = "test"
    async def complete(self, messages):
        assert "Schema" in messages[0].content
        return json.dumps({"action": "query", "query": {"action": "filter", "keywords": ["续约"]}})


class Main:
    provider_name = "test"
    model_name = "test"
    async def decide(self, **kwargs):
        return MainDecision(decision="delegate", domain="mail", intent="query_mail", instruction="查找续约邮件")


class Context:
    async def load_history(self, *args):
        return [{"role": "user", "content": "帮我找续约邮件"}]


class DecisionContext(DecisionContextService):
    async def load_references(self, run):
        return []
    async def record_decision(self, *args, **kwargs):
        pass


@pytest.mark.asyncio
async def test_specific_mail_request_only_returns_single_operation_card():
    """明确邮件的摘要请求只定位单封卡片，不能读取正文或生成摘要。"""
    from aegis_agent_worker.agent.contracts.mail import MailDecision
    from aegis_agent_worker.agent.contracts.decision import ResourceReference
    workflow, handler, runs, calls, snapshots, artifact_repo = assemble()
    target = ResourceReference(resource_type="email", id=UUID(int=31), source_version="v1")
    class Planner:
        provider_name, model_name = "test", "test"
        async def plan_mail(self, message, handoff):
            return MailDecision(action="query", query=MailQueryPlan(action="list"), target=handoff.target)
    workflow._agent = Planner()
    result = await workflow.build().ainvoke(dict(run_id=str(RUN.id), tenant_id=str(RUN.tenant_id),
        user_id=str(RUN.user_id), conversation_id=str(RUN.conversation_id), run_type="conversation",
        latest_user_message="帮我给出第一封邮件摘要",
        handoff=dict(domain="mail", intent="query_mail", instruction="定位第一封卡片",
                     target=target.model_dump(mode="json"))))
    artifact = artifact_repo.artifacts[0]
    assert artifact.payload["display_mode"] == "single"
    assert len(artifact.payload["items"]) == 1
    assert artifact.payload["items"][0]["email_id"] == str(target.id)
    assert "点击" in result["assistant_content"]
    assert handler.calls == 1
    assert all(event["event_type"] != "mail_extraction_completed" for event in runs.events)


def test_exact_subject_does_not_match_similar_mail():
    """完整主题请求可精确定位，不把主题相似的另一封邮件当成同一目标。"""
    plan = MailQueryPlan(action="filter", subject_equals="2026 年服务续约确认")
    assert plan.matches(MailListMessage.model_validate(message(subject="2026 年服务续约确认")))
    assert not plan.matches(MailListMessage.model_validate(message(subject="2026 年服务续约确认（补充）")))


def assemble(allowed=True, invalid=False, empty=False):
    database, runs, calls, snapshots, artifact_repo = Database(), Runs(), ToolCalls(), Snapshots(), ArtifactRepository()
    handler = Handler(invalid, empty)
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="mail.messages.list", version="1", risk_level="read", mcp_server="email",
        description="列表", connection_providers=("gmail",), required_connection_scope="mail.read"), handler)
    query = MailQueryService(database, runs, ToolGateway(registry, Connections(allowed)),
                             repository=snapshots, tool_calls=calls)
    artifact_service = ConversationArtifactService(database, runs, events=Publisher(), artifacts=artifact_repo)
    workflow = MailQueryWorkflow(MailAgent(Client()), query, artifact_service)
    return workflow, handler, runs, calls, snapshots, artifact_repo


@pytest.mark.asyncio
async def test_root_mail_query_synchronizes_metadata_filters_and_emits_artifact():
    """真实根图及查询子图组合：授权列表、同步全部快照、仅返回匹配项和刷新事件。"""
    workflow, handler, runs, calls, snapshots, artifact_repo = assemble()
    class Factory:
        def register(self, registry):
            registry.register("mail_query", workflow.build())
            registry.register_capability(ConversationCapability(
                domain="mail", intent="query_mail", workflow="mail_query", description="查询"))
    root = TaskGraph(TaskGraphDependencies(
        context_service=Context(), lifecycle=None, llm_client=None, system_prompt="",
        workflow_factories=(Factory(),), main_agent=Main(),
        decision_context_service=DecisionContext(None, None),
    )).build()
    result = await root.ainvoke(dict(run_id=str(RUN.id), tenant_id=str(RUN.tenant_id),
                                    user_id=str(RUN.user_id), conversation_id=str(RUN.conversation_id),
                                    run_type="conversation"))
    assert handler.calls == 1 and len(snapshots.messages) == 2
    artifact = artifact_repo.artifacts[0]
    assert artifact.artifact_type == "mail_candidates"
    assert artifact.payload["matched_count"] == 1
    assert artifact.payload["items"][0]["email_id"]
    assert artifact.payload["items"][0]["position"] == 1
    assert runs.events[-1]["event_type"] == "artifacts_ready"
    assert not any(event["event_type"] == "run_completed" for event in runs.events)
    assert result["assistant_content"] and "SECRET" not in json.dumps(result)
    assert "SECRET" not in json.dumps(calls.saved["response_payload"])


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed,invalid,empty", [(False, False, False), (True, True, False), (True, False, True)])
async def test_failure_is_not_empty_success_and_empty_success_has_artifact(allowed, invalid, empty):
    """权限或响应失败不生成空成功卡片，真实空结果仍保存候选 Artifact。"""
    workflow, handler, runs, calls, snapshots, artifact_repo = assemble(allowed, invalid, empty)
    result = await workflow.build().ainvoke(dict(run_id=str(RUN.id), tenant_id=str(RUN.tenant_id),
        user_id=str(RUN.user_id), conversation_id=str(RUN.conversation_id),
        latest_user_message="查询续约邮件",
        handoff=dict(domain="mail", intent="query_mail", instruction="查询")))
    if empty:
        assert artifact_repo.artifacts[0].payload["items"] == []
        assert result["tool_result"]["status"] == "succeeded"
    else:
        assert not artifact_repo.artifacts and calls.failed
        assert result["tool_result"]["status"] == "failed"
    if not allowed:
        assert handler.calls == 0
        assert any(event["event_type"] == "connection_required" for event in runs.events)


def test_query_plan_time_range_and_metadata_matching():
    """查询 AND 筛选、半开范围、邮箱条件及范围限制可预测且不依赖模型选择结果。"""
    plan = MailQueryPlan(action="filter", keywords=["续约"], sender_contains="customer",
                         start_at="2026-10-12T00:00:00+08:00", end_at="2026-10-13T00:00:00+08:00")
    assert plan.matches(MailListMessage.model_validate(message()))
    assert not plan.matches(MailListMessage.model_validate(message(received_at="2026-10-13T00:00:00+08:00")))
    with pytest.raises(ValueError):
        MailQueryPlan(action="filter", start_at="2026-10-12T00:00:00")
    with pytest.raises(ValueError):
        MailQueryPlan(action="list", keywords=["续约"], tool_name="mail.messages.send")


def test_artifact_count_and_byte_limits_are_explicit():
    """大结果卡片按数量与字节限制返回，不能把隐藏部分当成全部结果。"""
    rows = [dict(id=UUID(int=index+1), subject="续约"*80, sender_name="发件人"*20,
                 sender_email="a"*200+"@example.com", source_version="v1",
                 received_at=datetime.fromisoformat("2026-10-12T10:00:00+08:00"),
                 extraction_status="not_generated") for index in range(30)]
    artifact = build_mail_candidates(RUN, MailQueryPlan(action="list", limit=20), rows, 30, "mock")
    assert artifact.payload["truncated"]
    assert len(artifact.payload["items"]) <= 20
    assert artifact.payload["matched_count"] == 30


@pytest.mark.asyncio
async def test_latest_candidates_keep_card_order_and_do_not_fallback():
    """主助手指代顺序来自最近卡片；空查询和过期引用不换成收件箱第一封。"""
    class Result:
        def __init__(self, row):
            self.row = row
        def mappings(self):
            return self
        def first(self):
            return self.row
    class ArtifactSession:
        def __init__(self, artifact):
            self.artifact, self.calls = artifact, 0
        async def execute(self, sql, params):
            self.calls += 1
            assert params["tenant_id"] == RUN.tenant_id and params["user_id"] == RUN.user_id
            if self.calls == 1:
                return Result(self.artifact)
            return Result(dict(id=params["email_id"], subject="续约", source_version="v1"))
    rows = [dict(email_id=str(UUID(int=50)), position=1, source_version="v1"),
            dict(email_id=str(UUID(int=49)), position=2, source_version="v1")]
    session = ArtifactSession(dict(payload={"query_id": str(RUN.id), "items": rows}, expired=False))
    refs = await DecisionRepository().list_references(session, RUN)
    assert [item["id"] for item in refs] == [str(UUID(int=50)), str(UUID(int=49))]
    assert [item["position"] for item in refs] == [1, 2]
    for artifact in [dict(payload={"items":[]}, expired=False), dict(payload={"items":rows}, expired=True)]:
        session = ArtifactSession(artifact)
        assert await DecisionRepository().list_references(session, RUN) == []
        assert session.calls == 1
