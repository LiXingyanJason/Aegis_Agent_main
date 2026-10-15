"""供应商限制不能被扩大配置绕过，且调用前拒绝。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from pydantic import SecretStr
from aegis_agent_worker.embedding.client import EmbeddingClient,EmbeddingError


@pytest.mark.asyncio
async def test_ali_v4_limit_is_ten_even_if_configured_sixteen():
    settings=SimpleNamespace(embedding_enabled=True,embedding_provider='openai_compatible',embedding_model='text-embedding-v4',
        embedding_model_version='test-v1',embedding_dimensions=1024,embedding_max_batch_items=16)
    http=SimpleNamespace(post=AsyncMock())
    client=EmbeddingClient(settings,http)
    assert client.max_batch_items==10
    with pytest.raises(EmbeddingError) as error:
        await client.embed(['test']*11)
    assert error.value.code=='EMBEDDING_INPUT_LIMIT'
    http.post.assert_not_awaited()
