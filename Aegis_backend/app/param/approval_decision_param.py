"""逐项外部操作确认请求参数。"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ApprovalDecisionParam(BaseModel):
    """当前用户对一项待确认操作作出的明确决定。"""

    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def normalize_reason(self) -> "ApprovalDecisionParam":
        """拒绝时可选填写原因；批准时不接受无意义原因。"""
        if self.reason is not None:
            self.reason = self.reason.strip() or None
        return self
