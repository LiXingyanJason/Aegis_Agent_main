"""修改 Aegis 站内待办的请求参数。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class TodoUpdateParam(BaseModel):
    """允许用户编辑内容、截止时间，或更新待办的站内完成状态。"""

    content: str | None = Field(default=None, max_length=2000)
    due_at: datetime | None = None
    status: Literal["draft", "completed", "discarded"] | None = None

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str | None) -> str | None:
        """已提供的待办内容不得为空白。"""
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("待办内容不能为空")
        return normalized

    @model_validator(mode="after")
    def require_at_least_one_change(self):
        """拒绝空 PATCH，避免无意义地更新 updated_at。"""
        if not self.model_fields_set:
            raise ValueError("至少提供一个待修改字段")
        return self
