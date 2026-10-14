"""Embedding 通信与响应的稳定错误，不携带原始输入或响应。"""


class EmbeddingError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)
