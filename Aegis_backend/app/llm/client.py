"""OpenAI 兼容聊天补全客户端。"""

from dataclasses import dataclass
from typing import Any

import httpx

from app.config.settings import Settings


class LLMError(RuntimeError):
    """模型服务不可用、超时或返回格式异常时抛出。"""


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """发送给模型的一条标准聊天消息。"""

    role: str
    content: str


class OpenAICompatibleClient:
    """封装 OpenAI Chat Completions 兼容接口，隔离供应商 HTTP 细节。"""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client

    @property
    def provider_name(self) -> str:
        """返回写入任务追溯记录的模型供应商名称。"""
        return self._settings.model_provider

    @property
    def model_name(self) -> str:
        """返回写入任务追溯记录的模型名称。"""
        return self._settings.model_default_name

    async def complete(self, messages: list[LLMMessage]) -> str:
        """调用配置模型，并返回最终文本回复。"""
        # DeepSeek 的 Chat API 兼容 OpenAI Chat Completions 请求与响应格式，复用同一客户端。
        if self._settings.model_provider.lower() not in {
            "openai",
            "openai_compatible",
            "deepseek",
        }:
            raise LLMError(f"当前模型供应商尚未支持：{self._settings.model_provider}")

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._settings.model_request_timeout_seconds)
        try:
            response = await client.post(
                f"{str(self._settings.model_api_base).rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self._settings.model_api_key.get_secret_value()}"},
                json={
                    "model": self._settings.model_default_name,
                    "messages": [
                        {"role": message.role, "content": message.content} for message in messages
                    ],
                    "temperature": 0.2,
                },
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise LLMError("模型服务调用失败") from error
        finally:
            if owns_client:
                await client.aclose()

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LLMError("模型服务返回格式无效") from error
        if not isinstance(content, str) or not content.strip():
            raise LLMError("模型服务未返回有效文本")
        return content.strip()
