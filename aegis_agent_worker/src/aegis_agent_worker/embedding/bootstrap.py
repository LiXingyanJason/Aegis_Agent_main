"""启动组合点；6.3索引及6.4检索调用治理执行器，而不是直接调用通信客户端。"""
from aegis_agent_worker.harness.embedding_executor import EmbeddingExecutor
from aegis_agent_worker.embedding.client import EmbeddingClient


def create_embedding_executor(settings, database, *, client=None):
    return EmbeddingExecutor(database, EmbeddingClient(settings, client),
        external_queries_allowed=settings.embedding_external_queries_allowed)
