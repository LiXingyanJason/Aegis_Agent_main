"""向量模型空间契约；不依赖聊天客户端、治理或数据库。"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    provider: str
    model: str
    version: str
    dimensions: int
