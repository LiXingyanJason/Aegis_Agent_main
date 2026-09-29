"""模型客户端的测试。"""

import httpx
import pytest

from app.config.settings import Settings
from app.llm.client import LLMError, LLMMessage, OpenAICompatibleClient


def _settings() -> Settings:
    """构造不读取本地 .env 的模型客户端测试配置。"""
    return Settings(
        _env_file=None,
        database_url="postgresql://aegis:secret@localhost:5432/aegis_pa",
        redis_url="redis://localhost:6379/0",
        oidc_issuer_url="https://id.example.com/realms/aegis",
        oidc_audience="aegis-pa-api",
        oidc_client_id="aegis-pa-web",
        model_provider="openai",
        model_api_base="https://model.example.com/v1",
        model_api_key="test-key",
        model_default_name="test-model",
    )


@pytest.mark.asyncio
async def test_openai_compatible_client_returns_model_reply() -> None:
    """测试模型客户端发送标准消息并提取 Chat Completions 的文本回复。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        """验证模型请求后返回一个兼容格式的响应。"""
        assert request.url == "https://model.example.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "  已为你整理任务。  "}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAICompatibleClient(_settings(), http_client)
        reply = await client.complete([LLMMessage(role="user", content="整理任务")])

    assert reply == "已为你整理任务。"


@pytest.mark.asyncio
async def test_openai_compatible_client_rejects_invalid_model_response() -> None:
    """测试模型未返回 choices 文本时，客户端转换为受控 LLMError。"""

    async def handler(_request: httpx.Request) -> httpx.Response:
        """返回字段缺失的响应。"""
        return httpx.Response(200, json={"choices": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAICompatibleClient(_settings(), http_client)
        with pytest.raises(LLMError, match="返回格式"):
            await client.complete([LLMMessage(role="user", content="整理任务")])


@pytest.mark.asyncio
async def test_openai_compatible_client_supports_deepseek_provider() -> None:
    """测试 DeepSeek 可通过兼容的 Chat Completions 客户端调用。"""

    async def handler(_request: httpx.Request) -> httpx.Response:
        """返回 DeepSeek 兼容格式的聊天回复。"""
        return httpx.Response(200, json={"choices": [{"message": {"content": "可以处理。"}}]})

    values = _settings().model_dump()
    values["model_provider"] = "deepseek"
    settings = Settings(_env_file=None, **values)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAICompatibleClient(settings, http_client)
        reply = await client.complete([LLMMessage(role="user", content="测试")])

    assert reply == "可以处理。"
