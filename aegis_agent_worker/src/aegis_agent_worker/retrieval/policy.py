"""检索局部额度统一声明，Embedding仍使用独立持久化预算。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalPolicy:
    version: str = "hybrid-v1"
    max_versions: int = 200
    lexical_candidates: int = 20
    vector_candidates: int = 20
    max_query_terms: int = 128
    max_result_bytes: int = 12000
    rrf_k: int = 60
    allow_lexical_fallback: bool = False

    def __post_init__(self):
        if (not self.version or not 1 <= self.max_versions <= 1000
            or not 1 <= self.lexical_candidates <= 50 or not 1 <= self.vector_candidates <= 50
            or not 1 <= self.max_query_terms <= 512 or not 1 <= self.rrf_k <= 1000
            or not 1024 <= self.max_result_bytes <= 12000):
            raise ValueError("检索策略超出安全范围")
