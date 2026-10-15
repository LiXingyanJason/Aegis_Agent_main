"""向量索引协议；模型空间/权限过滤由可信调用方提供。"""
from typing import Protocol


class VectorIndexError(RuntimeError):
    def __init__(self,code,message):
        self.code=code
        super().__init__(message)


class VectorIndex(Protocol):
    async def ensure_collection(self,profile): ...
    async def upsert(self,points): ...
