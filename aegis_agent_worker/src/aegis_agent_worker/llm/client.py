"""OpenAI 兼容聊天补全客户端。"""

from dataclasses import dataclass
from contextvars import ContextVar
from typing import Any

import httpx

from aegis_agent_worker.config.settings import Settings
from aegis_agent_worker.llm.usage import TokenUsage


class LLMError(RuntimeError):
    """模型服务不可用、超时或返回格式异常时抛出。"""

    code = "LLM_UNAVAILABLE"


class LLMOutputError(LLMError):
    """请求成功但模型响应不可用；允许上层进行一次受预算约束的修复。"""

    code = "MODEL_OUTPUT_INVALID"


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
        self._usage = ContextVar("llm_token_usage", default=None)

    @property
    def usage(self) -> TokenUsage:
        """当前协程最近一次调用的用量，避免并发任务相互覆盖。"""
        return self._usage.get() or TokenUsage()

    def reset_usage(self) -> None:
        self._usage.set(None)

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
        return await self._complete(messages, json_mode=False)

    async def complete_json(self, messages: list[LLMMessage]) -> str:
        """请求 JSON 对象；业务 Schema 仍由 Agent 本地校验。"""
        return await self._complete(messages, json_mode=True)

    async def _complete(self, messages: list[LLMMessage], *, json_mode: bool) -> str:
        self.reset_usage()
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
                    **({"response_format": {"type": "json_object"}} if json_mode else {}),
                },
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise LLMError("模型服务调用失败") from error
        finally:
            if owns_client:
                await client.aclose()

        # 即使内容无效，供应商已报告的本次用量仍需要审计。
        self._usage.set(TokenUsage.from_payload(payload))
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LLMOutputError("模型服务返回格式无效") from error
        if not isinstance(content, str) or not content.strip():
            raise LLMOutputError("模型服务未返回有效文本")
        return content.strip()
