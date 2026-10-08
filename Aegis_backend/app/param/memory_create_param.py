"""创建用户长期记忆的请求参数。"""

import re

from pydantic import BaseModel, Field, field_validator


_HIGH_RISK_SECRET_PATTERN = re.compile(
    r"(?:password|密码|api[_ -]?key|密钥|private[_ -]?key|私钥|银行卡|card[_ -]?number)\s*[:：=]",
    re.IGNORECASE,
)


class MemoryCreateParam(BaseModel):
    """用户手动录入或明确确认的一条长期偏好。"""

    content: str = Field(min_length=1, max_length=4000)
    is_sensitive: bool = False

    @field_validator("content")
    @classmethod
    def normalize_and_reject_secrets(cls, value: str) -> str:
        """拒绝空白与明显的高危秘密，避免把凭据当作长期记忆保存。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("记忆内容不能为空")
        if _HIGH_RISK_SECRET_PATTERN.search(normalized):
            raise ValueError("长期记忆不允许保存密码、API Key、私钥或完整银行卡信息")
        return normalized
