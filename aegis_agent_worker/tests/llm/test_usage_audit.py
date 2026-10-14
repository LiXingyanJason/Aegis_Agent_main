"""Mock HTTP 验证逐次用量、失败记录、修复用途和协程隔离，不连接外部服务。"""
import asyncio
import httpx
import pytest

from test_client import _settings
from test_harness_layers import Database, Repository, RUN
from aegis_agent_worker.llm.client import OpenAICompatibleClient, LLMMessage, LLMError, LLMOutputError
from aegis_agent_worker.llm.guarded_client import GuardedLLMClient
from aegis_agent_worker.common.usage import TokenUsage
from aegis_agent_worker.harness.executor import HarnessExecutor
from aegis_agent_worker.agent.specialists.main_agent import MainAgent
from aegis_agent_worker.agent.contracts.decision import ConversationCapability


@pytest.mark.parametrize("payload,expected", [
    ({}, TokenUsage()),
    ({"usage": None}, TokenUsage()),
    ({"usage": {"prompt_tokens": 0, "completion_tokens": 3, "total_tokens": 3}}, TokenUsage(0, 3, 3)),
    ({"usage": {"prompt_tokens": 7}}, TokenUsage(7)),
    ({"usage": {"prompt_tokens": True, "completion_tokens": -1, "total_tokens": "10"}}, TokenUsage()),
    ({"usage": {"prompt_tokens": 1.5, "total_tokens": 2**64}}, TokenUsage()),
])
def test_optional_counts_are_not_estimated(payload, expected):
    assert TokenUsage.from_payload(payload) == expected


@pytest.mark.asyncio
async def test_each_call_records_usage_and_resets_after_failure():
    responses = [
        {"choices": [{"message": {"content": "ok"}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
        {"choices": [], "usage": {"total_tokens": 4}},
        None,
        {"choices": [{"message": {"content": "ok"}}]},
    ]
    async def handler(request):
        payload = responses.pop(0)
        return httpx.Response(503 if payload is None else 200, json=payload)
    repo = Repository()
    harness = HarnessExecutor(Database(), repository=repo)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GuardedLLMClient(OpenAICompatibleClient(_settings(), http), harness)
        async with harness.scope(RUN):
            assert await client.complete([LLMMessage("user", "private")]) == "ok"
            with pytest.raises(LLMOutputError):
                await client.complete_json([])
            with pytest.raises(LLMError):
                await client.complete([])
            assert await client.complete([]) == "ok"
    assert len(repo.reservations) == len(repo.finished) == 4
    first, invalid, network, missing = repo.finished
    assert (first["prompt_tokens"], first["completion_tokens"], first["total_tokens"]) == (10, 2, 12)
    assert first["purpose"] == "model_completion" and first["model_name"] == "test-model"
    assert invalid["status"] == "failed" and invalid["total_tokens"] == 4
    assert network["error_code"] == "LLM_UNAVAILABLE" and network["total_tokens"] is None
    assert missing["prompt_tokens"] is missing["total_tokens"] is None
    assert "private" not in str(repo.finished)


@pytest.mark.asyncio
async def test_main_route_and_format_repair_have_separate_usage():
    outputs = ["not JSON", '{"decision":"reply"}']
    async def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": outputs.pop(0)}}],
                                       "usage": {"total_tokens": 5}})
    repo = Repository()
    harness = HarnessExecutor(Database(), repository=repo)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        main = MainAgent(GuardedLLMClient(OpenAICompatibleClient(_settings(), http), harness))
        async with harness.scope(RUN):
            result = await main.decide(message="你好", history=[], references=[], capabilities=[
                ConversationCapability(domain="calendar", intent="list_events",
                                       workflow="calendar_read", description="查询").model_dump()])
    assert result.decision == "reply"
    assert [item["purpose"] for item in repo.finished] == ["main_routing", "main_routing_repair"]
    assert [item["total_tokens"] for item in repo.finished] == [5, 5]
    assert main.output_metadata["repair_count"] == 1


@pytest.mark.asyncio
async def test_usage_isolated_between_concurrent_tasks():
    async def handler(request):
        import json
        count = int(json.loads(request.content)["messages"][0]["content"])
        await asyncio.sleep(0)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                       "usage": {"total_tokens": count}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleClient(_settings(), http)
        async def call(count):
            await client.complete([LLMMessage("user", str(count))])
            await asyncio.sleep(0)
            return client.usage.total_tokens
        assert await asyncio.gather(call(3), call(8)) == [3, 8]


@pytest.mark.asyncio
async def test_repository_persists_only_safe_metadata_and_unknown_tokens():
    from uuid import UUID
    from aegis_agent_worker.repository.execution_repository import ExecutionRepository
    class Session:
        async def execute(self, sql, args):
            self.sql, self.args = str(sql), args
    session = Session()
    await ExecutionRepository().finish_attempt(session, UUID(int=1), status="succeeded", error_code=None,
                                               purpose="main_routing", model_provider="deepseek",
                                               model_name="test", total_tokens=10)
    assert "prompt_tokens=:prompt" in session.sql and "completion_tokens=:completion" in session.sql
    assert session.args["prompt"] is session.args["completion"] is None
    assert session.args["total"] == 10


@pytest.mark.asyncio
async def test_route_event_remains_safe_and_records_fallback_reason():
    from aegis_agent_worker.service.runtime.decision_context_service import DecisionContextService
    from aegis_agent_worker.agent.contracts.decision import MainDecision
    class Runs:
        async def append_event(self, session, **kwargs):
            self.event = kwargs
    runs = Runs()
    service = DecisionContextService(Database(), runs)
    await service.record_decision(RUN, decision=MainDecision(decision="clarify", clarification_question="秘密正文"),
        outcome="invalid_output", fallback_reason="invalid_output", duration_ms=10, capabilities=[],
        model_provider="test", model_name="test", route_policy_version="selector-v1",
        candidate_capabilities=["mail.query_mail"], filter_reason_codes=["ambiguous"], repair_count=1,
        validation_issue_codes=[{"path": "decision", "type": "literal_error"}])
    assert runs.event["event_type"] == "main_decision_completed"
    payload = runs.event["payload"]
    assert payload["fallback_reason"] == "invalid_output" and payload["repair_count"] == 1
    assert payload["route_policy_version"] == "selector-v1"
    assert "秘密正文" not in str(payload)
