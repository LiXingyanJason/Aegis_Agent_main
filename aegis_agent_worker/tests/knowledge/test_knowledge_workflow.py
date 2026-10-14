"""分层替身验证，不代表PostgreSQL/RLS/Hybrid检索已实现。"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from aegis_agent_worker.agent.contracts.knowledge import KnowledgeQueryPlan, KnowledgeSearchResult, RetrievedEvidence, KnowledgeAnswer
from aegis_agent_worker.agent.contracts.decision import MainDecision
from aegis_agent_worker.agent.specialists.knowledge_agent import KnowledgeAgent, KnowledgeOutputError
from aegis_agent_worker.agent.workflows.knowledge.query_workflow import KnowledgeQueryWorkflow
from aegis_agent_worker.agent.workflows.knowledge.factory import KnowledgeWorkflowFactory
from aegis_agent_worker.service.knowledge.retrieval_service import RetrievalService
from aegis_agent_worker.service.knowledge.evidence_service import EvidenceService
from aegis_agent_worker.tool.knowledge.handler import KnowledgeSearchHandler
from aegis_agent_worker.tool.contracts import ToolContext, ToolError, ToolInvocation
from aegis_agent_worker.tool.registry import ToolRegistry
from aegis_agent_worker.tool.gateway import ToolGateway
from aegis_agent_worker.tool.contracts import ToolDefinition
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.harness.policy_provider import PolicyProvider, LEGACY_POLICY, KNOWLEDGE_CONTRACT_POLICY
from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
from aegis_agent_worker.llm.client import LLMError
from aegis_agent_worker.agent.graph.graph_registry import GraphRegistry
from aegis_agent_worker.agent.graph.task_graph import TaskGraph, TaskGraphDependencies
from test_routing_baseline import Main, Context, DecisionContext, LLM, Lifecycle, initial
from test_harness_layers import Database, Repository
from aegis_agent_worker.repository.run_repository import ClaimedRun

FIXTURES = json.loads((Path(__file__).parent/"data/documents.json").read_text(encoding="utf-8"))


def evidence():
    doc = FIXTURES["documents"][1]
    return RetrievedEvidence(source_no=1, document_id=doc["id"], version_id=UUID(int=201), chunk_id=UUID(int=301),
        title=doc["title"], version_label="v1", section="通知期限", start_offset=0, end_offset=len(doc["text"]),
        excerpt=doc["text"], content_hash=hashlib.sha256(doc["text"].encode()).hexdigest())


def state():
    data, _ = initial()
    return {**data, "latest_user_message":"续约要提前多久？", "handoff":{
        "domain":"knowledge", "intent":"query_knowledge", "instruction":"查询续约通知期限"}}


class KnowledgeStore:
    def __init__(self, mode="valid"):
        self.mode, self.calls = mode, []
    def authorized(self, context):
        return context.tenant_id == UUID(int=2) and context.user_id == UUID(int=3)
    async def retrieve(self, context, plan):
        self.calls.append((context, plan))
        return [evidence()] if self.mode != "empty" and self.authorized(context) else []
    async def revalidate(self, context, rows):
        if self.mode == "revoked" or not self.authorized(context):
            return []
        if self.mode == "changed":
            return [evidence().model_copy(update={"version_id": UUID(int=202)})]
        return [item for item in rows if item == evidence()]


class Tools:
    def __init__(self, store):
        self.calls = 0
        registry = ToolRegistry()
        registry.register(ToolDefinition("knowledge.search", "1", "read", "internal", "查询",
            argument_schema=KnowledgeQueryPlan), KnowledgeSearchHandler(RetrievalService(store)))
        self.gateway = ToolGateway(registry)
    async def query(self, run, plan):
        self.calls += 1
        prepared = await self.gateway.prepare(None, tenant_id=run.tenant_id, user_id=run.user_id,
                                             run_id=run.id, invocation=plan.to_invocation())
        result = await self.gateway.invoke(prepared)
        return KnowledgeSearchResult.model_validate(result.response_payload)


class Agent:
    def __init__(self, invalid=False, forged=False):
        self.answers, self.invalid, self.forged = 0, invalid, forged
    async def plan_query(self, *args):
        return {"query":"查询", "tenant_id":"bad"} if self.invalid else KnowledgeQueryPlan(query="续约通知期限")
    async def generate_answer(self, query, rows):
        self.answers += 1
        number = 2 if self.forged else 1
        return KnowledgeAnswer(answer=f"至少提前30天通知。[{number}]", citation_numbers=[number], can_confirm=True)


class Artifacts:
    def __init__(self): self.saved = []
    async def persist(self, run, values): self.saved.extend(values)


@pytest.mark.asyncio
async def test_layered_success_produces_verified_source_artifact():
    store, agent, artifacts = KnowledgeStore(), Agent(), Artifacts()
    tools = Tools(store)
    result = await KnowledgeQueryWorkflow(agent, tools, EvidenceService(store), artifacts).build().ainvoke(state())
    assert result["knowledge_status"] == "succeeded" and tools.calls == agent.answers == 1
    assert result["assistant_content"] == "至少提前30天通知。[1]"
    assert artifacts.saved[0].artifact_type == "knowledge_sources"
    assert artifacts.saved[0].payload["sources"][0]["version_id"] == str(UUID(int=201))
    assert "tenant_id" not in artifacts.saved[0].payload["sources"][0]
    assert "excerpt" not in str(result.get("tool_result", {}))


@pytest.mark.asyncio
async def test_invalid_plan_has_zero_retrieval_calls():
    tools = Tools(KnowledgeStore())
    result = await KnowledgeQueryWorkflow(Agent(invalid=True), tools).build().ainvoke(state())
    assert result["knowledge_status"] == "invalid_plan" and tools.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["empty", "revoked", "changed"])
async def test_empty_stale_or_revoked_evidence_never_calls_answer(mode):
    store, agent, artifacts = KnowledgeStore(mode), Agent(), Artifacts()
    result = await KnowledgeQueryWorkflow(agent, Tools(store), EvidenceService(store), artifacts).build().ainvoke(state())
    assert result["knowledge_status"] == "insufficient_evidence" and agent.answers == 0
    assert not artifacts.saved


@pytest.mark.asyncio
async def test_forged_citation_is_not_persisted():
    store, artifacts = KnowledgeStore(), Artifacts()
    result = await KnowledgeQueryWorkflow(Agent(forged=True), Tools(store), EvidenceService(store), artifacts).build().ainvoke(state())
    assert result["knowledge_error_code"] == "KNOWLEDGE_INVALID_ANSWER" and not artifacts.saved


@pytest.mark.asyncio
async def test_permission_revoked_during_generation_blocks_delivery():
    store, artifacts = KnowledgeStore(), Artifacts()
    class RevokingAgent(Agent):
        async def generate_answer(self, query, rows):
            store.mode = "revoked"
            return await super().generate_answer(query, rows)
    result = await KnowledgeQueryWorkflow(RevokingAgent(), Tools(store), EvidenceService(store), artifacts).build().ainvoke(state())
    assert result["knowledge_error_code"] == "KNOWLEDGE_SOURCE_UNAVAILABLE" and not artifacts.saved


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [ToolContext(UUID(int=4), UUID(int=3), UUID(int=1)),
                                     ToolContext(UUID(int=2), UUID(int=4), UUID(int=1))])
async def test_fake_repository_permission_matrix(context):
    assert not (await RetrievalService(KnowledgeStore()).search(context, KnowledgeQueryPlan(query="政策"))).evidence


@pytest.mark.asyncio
async def test_missing_tool_and_unconfigured_service_do_not_fabricate():
    class Missing:
        async def query(self, *args):
            raise ToolError("TOOL_NOT_REGISTERED", "工具缺失")
    agent = Agent()
    result = await KnowledgeQueryWorkflow(agent, Missing()).build().ainvoke(state())
    assert result["knowledge_error_code"] == "TOOL_NOT_REGISTERED" and agent.answers == 0
    with pytest.raises(ToolError) as error:
        await RetrievalService().search(ToolContext(UUID(int=2), UUID(int=3), UUID(int=1)), KnowledgeQueryPlan(query="政策"))
    assert error.value.code == "KNOWLEDGE_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_gateway_rejects_identity_before_handler_and_default_handler_is_unconfigured():
    from aegis_agent_worker.config.settings import Settings
    from aegis_agent_worker.tool.bootstrap import create_default_tool_gateway
    settings = Settings(_env_file=None, database_url="postgresql://unused/unused", redis_url="redis://unused",
        model_provider="openai", model_api_base="https://example.invalid", model_api_key="offline", model_default_name="offline")
    gateway = create_default_tool_gateway(settings)
    with pytest.raises(ToolError) as error:
        await gateway.prepare(None, tenant_id=UUID(int=2), user_id=UUID(int=3), run_id=UUID(int=1),
            invocation=ToolInvocation("knowledge.search", {"query":"政策", "tenant_id":"other"}))
    assert error.value.code == "TOOL_INVALID_ARGUMENTS"
    prepared = await gateway.prepare(None, tenant_id=UUID(int=2), user_id=UUID(int=3), run_id=UUID(int=1),
        invocation=KnowledgeQueryPlan(query="政策").to_invocation())
    with pytest.raises(ToolError) as error:
        await gateway.invoke(prepared)
    assert error.value.code == "KNOWLEDGE_NOT_CONFIGURED" and prepared.context.connection_id is None


@pytest.mark.asyncio
async def test_new_tool_policy_does_not_modify_old_version():
    from aegis_agent_worker.harness.exceptions import HarnessError
    assert "knowledge.search" not in LEGACY_POLICY.execution.tools("conversation")
    run = ClaimedRun(UUID(int=1), UUID(int=2), UUID(int=3), UUID(int=4))
    legacy = HarnessExecutor(Database(), repository=Repository())
    async with legacy.scope(run):
        with pytest.raises(HarnessError) as error:
            async with legacy.attempt("tool", "knowledge.search"):
                pytest.fail("旧策略不能执行新工具")
        assert error.value.code == "TOOL_SCOPE_BLOCKED"
    policies = PolicyProvider((LEGACY_POLICY, KNOWLEDGE_CONTRACT_POLICY), KNOWLEDGE_CONTRACT_POLICY.version)
    current = HarnessExecutor(Database(), repository=Repository(), policy_provider=policies)
    async with current.scope(run):
        async with current.attempt("tool", "knowledge.search"):
            pass


@pytest.mark.asyncio
async def test_guarded_real_expert_with_mock_model_and_cumulative_budget():
    class Model:
        provider_name = model_name = "offline"
        calls = 0
        async def complete(self, messages):
            return await self.complete_json(messages)
        async def complete_json(self, messages):
            self.calls += 1
            if self.calls == 1: return "invalid JSON"
            if self.calls == 2: return '{"query":"续约政策"}'
            return '{"answer":"提前30天通知。[1]","citation_numbers":[1],"can_confirm":true}'
    model, repo = Model(), Repository()
    policies = PolicyProvider((LEGACY_POLICY, KNOWLEDGE_CONTRACT_POLICY), KNOWLEDGE_CONTRACT_POLICY.version)
    harness = HarnessExecutor(Database(), repository=repo, policy_provider=policies)
    agent = KnowledgeAgent(GuardedLLMClient(model, harness), policies)
    run = ClaimedRun(UUID(int=1), UUID(int=2), UUID(int=3), UUID(int=4))
    async with harness.scope(run):
        from aegis_agent_worker.agent.contracts.decision import AgentHandoff
        plan = await agent.plan_query("政策", AgentHandoff(domain="knowledge", intent="query_knowledge", instruction="政策"))
        answer = await agent.generate_answer(plan.query, [evidence()])
    assert answer.can_confirm and model.calls == len(repo.reservations) == 3


@pytest.mark.asyncio
async def test_network_failure_is_not_repaired():
    class Model:
        calls = 0
        async def complete(self, messages):
            self.calls += 1
            raise LLMError("offline")
    model = Model()
    agent = KnowledgeAgent(model)
    from aegis_agent_worker.agent.contracts.decision import AgentHandoff
    with pytest.raises(LLMError):
        await agent.plan_query("政策", AgentHandoff(domain="knowledge", intent="query_knowledge", instruction="政策"))
    assert model.calls == 1


@pytest.mark.asyncio
async def test_factory_disabled_by_default_and_root_can_route_when_explicitly_enabled():
    store, artifacts = KnowledgeStore(), Artifacts()
    factory = KnowledgeWorkflowFactory(Agent(), Tools(store), EvidenceService(store), artifacts)
    registry = GraphRegistry()
    factory.register(registry)
    assert not registry.capabilities()
    from dataclasses import replace
    main = Main(MainDecision(decision="delegate", domain="knowledge", intent="query_knowledge", instruction="续约政策"))
    graph = TaskGraph(TaskGraphDependencies(Context(), Lifecycle(), LLM(), "规则", (replace(factory, enabled=True),),
        main_agent=main, decision_context_service=DecisionContext())).build()
    value, _ = initial()
    result = await graph.ainvoke(value)
    assert result["knowledge_status"] == "succeeded" and result["workflow"] == "knowledge_query"
