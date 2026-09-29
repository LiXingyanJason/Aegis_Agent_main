"""发送会话消息的请求参数模型。"""

from pydantic import BaseModel, Field, field_validator


class MessageSendParam(BaseModel):
    """用户向既有会话提交一条任务消息时的参数。"""

    content: str = Field(min_length=1, max_length=8000, description="用户输入的任务内容")
    client_message_id: str = Field(
        min_length=1,
        max_length=128,
        description="前端生成的消息唯一标识，用于防止重复发送",
    )

    @field_validator("content", "client_message_id")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        """去除首尾空白，并拒绝只由空白字符组成的输入。"""
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("字段不能为空白")
        return normalized_value
