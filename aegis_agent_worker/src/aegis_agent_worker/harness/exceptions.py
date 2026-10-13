"""执行治理稳定错误，不携带原始模型输入或工具响应。"""


class HarnessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
