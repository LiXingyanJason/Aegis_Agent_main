"""Embedding 专属且不可变的版本化策略，不修改已发布聊天策略。"""
from dataclasses import dataclass
from types import MappingProxyType
from aegis_agent_worker.harness.exceptions import HarnessError


@dataclass(frozen=True, slots=True)
class EmbeddingPolicy:
    version: str
    purpose: str
    max_calls: int
    max_total_input_chars: int
    max_batch_items: int
    max_batch_chars: int
    timeout_seconds: float

    def __post_init__(self):
        if self.purpose not in {"knowledge_index", "knowledge_query"}:
            raise ValueError("未知 Embedding 用途")
        if not self.version or any(value <= 0 for value in (self.max_calls, self.max_total_input_chars,
            self.max_batch_items, self.max_batch_chars, self.timeout_seconds)):
            raise ValueError("Embedding 策略额度必须为正数")


INDEX_POLICY = EmbeddingPolicy("embedding-index-v1", "knowledge_index", 128, 2_000_000, 16, 16000, 30)
QUERY_POLICY = EmbeddingPolicy("embedding-query-v1", "knowledge_query", 4, 8000, 3, 2000, 15)


class EmbeddingPolicyProvider:
    def __init__(self, profiles=(INDEX_POLICY, QUERY_POLICY)):
        versions = {profile.version: profile for profile in profiles}
        if len(versions) != len(profiles):
            raise ValueError("Embedding 策略版本重复")
        self._profiles = MappingProxyType(versions)

    def current(self, purpose):
        return self.resolve({"knowledge_index": INDEX_POLICY.version,
                             "knowledge_query": QUERY_POLICY.version}[purpose])

    def resolve(self, version):
        if version not in self._profiles:
            raise HarnessError("EMBEDDING_POLICY_UNAVAILABLE", "Embedding 策略版本不可用")
        return self._profiles[version]
