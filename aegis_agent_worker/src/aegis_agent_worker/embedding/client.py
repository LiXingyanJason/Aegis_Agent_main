"""独立 Embeddings 协议适配；不调用聊天接口，也不生成占位向量。"""
from contextvars import ContextVar
from math import isfinite
import httpx
from aegis_agent_worker.common.usage import TokenUsage
from aegis_agent_worker.embedding.contracts import EmbeddingProfile
from aegis_agent_worker.embedding.exceptions import EmbeddingError


class EmbeddingClient:
    def __init__(self, settings, client: httpx.AsyncClient | None = None):
        self._settings, self._client = settings, client
        self._usage = ContextVar("embedding_usage", default=None)

    @property
    def profile(self):
        # @property 把一个方法变成像字段一样读取的属性。client.EmbeddingProfile
        # 返回当前 Embedding 模型的供应商、名称、版本和向量维度。
        self.ensure_configured()
        return EmbeddingProfile(self._settings.embedding_provider, self._settings.embedding_model,
                                self._settings.embedding_model_version, self._settings.embedding_dimensions)

    def ensure_configured(self):
        if not self._settings.embedding_enabled:
            raise EmbeddingError("EMBEDDING_NOT_CONFIGURED", "Embedding 服务尚未配置或启用")

    @property
    def max_batch_items(self):
        configured=getattr(self._settings,'embedding_max_batch_items',10)
        return min(configured,10) if self._settings.embedding_model=='text-embedding-v4' else configured

    def reset_usage(self):
        self._usage.set(None)

    @property
    def usage(self):
        return self._usage.get() or TokenUsage()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """校验输入、请求模型服务、记录返回的用量、验证向量，并按输入顺序返回向量列表"""
        self.reset_usage()
        profile = self.profile
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingError("EMBEDDING_INPUT_INVALID", "Embedding 输入不能为空")
        if len(texts)>self.max_batch_items:
            raise EmbeddingError('EMBEDDING_INPUT_LIMIT','超过供应商批次上限')
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(f"{str(self._settings.embedding_api_base).rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {self._settings.embedding_api_key.get_secret_value()}"},
                json={"model": profile.model, "input": texts, "encoding_format": "float",
                      **({"dimensions": profile.dimensions} if self._settings.embedding_send_dimensions else {})},
                timeout=self._settings.embedding_request_timeout_seconds)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException:
            raise EmbeddingError("EMBEDDING_TIMEOUT", "Embedding 请求超时") from None
        except (httpx.HTTPError, ValueError):
            raise EmbeddingError("EMBEDDING_UNAVAILABLE", "Embedding 服务调用失败") from None
        finally:
            if owns_client:
                await client.aclose()
        self._usage.set(TokenUsage.from_payload(payload))
        try:
            data = payload["data"]
            if not isinstance(data, list) or len(data) != len(texts):
                raise ValueError()
            ordered = {}
            for row in data:
                index, vector = row["index"], row["embedding"]
                if type(index) is not int or index not in range(len(texts)) or index in ordered:
                    raise ValueError()
                if not isinstance(vector, list) or len(vector) != profile.dimensions:
                    raise EmbeddingError("EMBEDDING_DIMENSION_MISMATCH", "Embedding 维度与配置不一致")
                if any(type(value) not in (int, float) or not isfinite(value) for value in vector) or not any(vector):
                    raise ValueError()
                ordered[index] = [float(value) for value in vector]
            return [ordered[index] for index in range(len(texts))]
        except (KeyError, TypeError, ValueError, OverflowError):
            raise EmbeddingError("EMBEDDING_RESPONSE_INVALID", "Embedding 返回的向量无效") from None
