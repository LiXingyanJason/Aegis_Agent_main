"""创建会话接口的请求参数。"""

from pydantic import BaseModel, Field, field_validator


class ConversationCreateParam(BaseModel):
    """用户创建一个新任务会话时提交的参数。"""

    title: str | None = Field(default=None, max_length=200, description="会话标题，可选")

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        """去除标题首尾空白；空标题按未填写处理。"""
        if value is None:
            return None
        normalized_value = value.strip()
        return normalized_value or None
