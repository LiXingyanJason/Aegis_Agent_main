"""Embedding通信包与聊天包的依赖边界。"""
import ast
from pathlib import Path

from aegis_agent_worker.common.usage import TokenUsage
from aegis_agent_worker.embedding import client
from aegis_agent_worker.embedding.contracts import EmbeddingProfile
from aegis_agent_worker.embedding.exceptions import EmbeddingError
from aegis_agent_worker.llm import client as chat_client


def test_clients_share_usage_but_embedding_contracts_are_independent():
    assert client.TokenUsage is chat_client.TokenUsage is TokenUsage
    assert EmbeddingProfile.__module__ == "aegis_agent_worker.embedding.contracts"
    assert EmbeddingError.__module__ == "aegis_agent_worker.embedding.exceptions"


def test_embedding_package_does_not_import_chat_modules():
    for path in Path(client.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("aegis_agent_worker.llm"), path.name
            elif isinstance(node, ast.Import):
                assert not any(item.name.startswith("aegis_agent_worker.llm") for item in node.names), path.name
