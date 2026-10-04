"""服务内部使用的受控业务异常。"""


class MCPError(RuntimeError):
    """可安全转换为 JSON-RPC 错误响应的异常。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
