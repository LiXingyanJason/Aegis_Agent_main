"""请求生成邮件回复草稿的参数。"""

from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class MailReplyDraftRequestParam(BaseModel):
    """可使用已有摘要结果辅助起草，也可仅依据原邮件起草。"""

    extraction_id: UUID | None = None
    instruction: str | None = Field(default=None, max_length=2000)

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str | None) -> str | None:
        """空白补充要求按未提供处理。"""
        return value.strip() or None if value is not None else None
