"""Embedding 通信和治理离线测试；不请求真实供应商。"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import httpx
import pytest
from pydantic import SecretStr
from aegis_agent_worker.embedding.client import EmbeddingClient
from aegis_agent_worker.embedding.exceptions import EmbeddingError
from aegis_agent_worker.harness.embedding_executor import EmbeddingExecutor, EmbeddingSubject
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.harness.policies.embedding import EmbeddingPolicyProvider, QUERY_POLICY
from aegis_agent_worker.config.settings import Settings
from test_knowledge_query_service import Database


def settings(**overrides):
    return SimpleNamespace(embedding_enabled=True, embedding_provider="openai_compatible",
        embedding_api_base="https://embedding.invalid/v1", embedding_api_key=SecretStr("test-only"),
        embedding_model="test-model", embedding_model_version="test-v1", embedding_dimensions=3,
        embedding_send_dimensions=False, embedding_request_timeout_seconds=2, **overrides)


def payload():
    return {"data": [{"index": 0, "embedding": [1, 2, 3]}], "usage": {"prompt_tokens": 5, "total_tokens": 5}}


@pytest.mark.asyncio
async def test_embedding_protocol_and_usage():
    def handler(request):
        assert request.url.path == "/v1/embeddings"
        assert b"messages" not in request.content
        return httpx.Response(200, json=payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = EmbeddingClient(settings(), http)
        assert await client.embed(["虚构资料"]) == [[1.0, 2.0, 3.0]]
        assert client.usage.total_tokens == 5 and client.usage.completion_tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize("vector,code", [([1, 2], "EMBEDDING_DIMENSION_MISMATCH"),
    ([True, 2, 3], "EMBEDDING_RESPONSE_INVALID"), ([0, 0, 0], "EMBEDDING_RESPONSE_INVALID")])
async def test_invalid_vectors_keep_reported_usage(vector, code):
    raw = payload()
    raw["data"][0]["embedding"] = vector
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw))) as http:
        client = EmbeddingClient(settings(), http)
        with pytest.raises(EmbeddingError) as error:
            await client.embed(["资料"])
        assert error.value.code == code and client.usage.total_tokens == 5


@pytest.mark.asyncio
async def test_missing_usage_stays_unknown_and_missing_service_fails():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": payload()["data"]}))) as http:
        client = EmbeddingClient(settings(), http)
        await client.embed(["资料"])
        assert client.usage.total_tokens is None
        client._settings.embedding_enabled = False
        with pytest.raises(EmbeddingError, match="尚未配置"):
            await client.embed(["资料"])


def governed(http, policy=QUERY_POLICY):
    database = Database()
    client = EmbeddingClient(settings(), http)
    repo = SimpleNamespace(authorize=AsyncMock(),
        ensure_budget=AsyncMock(return_value={"id": UUID(int=7), "policy_version": policy.version}),
        reserve=AsyncMock(), start=AsyncMock(return_value=UUID(int=8)), finish=AsyncMock())
    return EmbeddingExecutor(database, client, repository=repo,
        policies=EmbeddingPolicyProvider((policy,)), external_queries_allowed=True), repo, database


SUBJECT = EmbeddingSubject("run", UUID(int=1), UUID(int=2), UUID(int=3))


@pytest.mark.asyncio
async def test_governed_call_is_outside_transaction_and_audited():
    database = None
    def handler(request):
        assert not database.active
        return httpx.Response(200, json=payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        executor, repo, database = governed(http)
        await executor.embed(SUBJECT, ["资料"])
        assert repo.reserve.await_args.args[-1] == 2
        assert repo.finish.await_args.kwargs["usage"].total_tokens == 5


@pytest.mark.asyncio
async def test_over_budget_does_not_call_network():
    http = SimpleNamespace(post=AsyncMock())
    executor, repo, _ = governed(http)
    repo.reserve.side_effect = HarnessError("EMBEDDING_BUDGET_EXCEEDED", "耗尽")
    with pytest.raises(HarnessError):
        await executor.embed(SUBJECT, ["资料"])
    http.post.assert_not_awaited()
    repo.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_is_stable_and_audited():
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as http:
        executor, repo, _ = governed(http, replace(QUERY_POLICY, timeout_seconds=0.01))
        with pytest.raises(HarnessError) as error:
            await executor.embed(SUBJECT, ["资料"])
        assert error.value.code == repo.finish.await_args.kwargs["code"] == "EMBEDDING_TIMEOUT"


@pytest.mark.asyncio
async def test_batch_and_authorization_block_before_network():
    http = SimpleNamespace(post=AsyncMock())
    executor, repo, _ = governed(http)
    with pytest.raises(HarnessError) as error:
        await executor.embed(SUBJECT, ["资料"]*4)
    assert error.value.code == "EMBEDDING_INPUT_LIMIT"
    repo.authorize.side_effect = HarnessError("EMBEDDING_SUBJECT_DENIED", "不允许外发")
    with pytest.raises(HarnessError):
        await executor.embed(SUBJECT, ["资料"])
    http.post.assert_not_awaited()


def test_enabled_requires_separate_embedding_configuration():
    with pytest.raises(ValueError):
        Settings(_env_file=None, database_url="postgresql://test", redis_url="redis://test",
            model_provider="deepseek", model_api_base="https://test.invalid", model_api_key="test",
            model_default_name="chat", embedding_enabled=True)


@pytest.mark.asyncio
async def test_external_query_opt_in_is_required():
    http = SimpleNamespace(post=AsyncMock())
    executor, repo, _ = governed(http)
    executor._external_queries_allowed = False
    with pytest.raises(HarnessError) as error:
        await executor.embed(SUBJECT, ["资料"])
    assert error.value.code == "EMBEDDING_DATA_POLICY_BLOCKED"
    repo.authorize.assert_not_awaited()
    http.post.assert_not_awaited()
