"""保存邮件草稿修改时的请求参数。"""

from pydantic import BaseModel, Field, field_validator


class MailDraftUpdateParam(BaseModel):
    """用户保存已存在邮件草稿的收件人、主题和正文。"""

    to: list[str] = Field(min_length=1, max_length=50)
    cc: list[str] = Field(default_factory=list, max_length=50)
    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=65536)

    @field_validator("to", "cc")
    @classmethod
    def normalize_recipients(cls, values: list[str]) -> list[str]:
        """去除空白、拒绝明显无效地址，并在同一类型中去重。"""
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            email = value.strip().lower()
            if not email or "@" not in email or email.startswith("@") or email.endswith("@"):
                raise ValueError("收件人邮箱格式无效")
            if email not in seen:
                seen.add(email)
                normalized.append(email)
        return normalized

    @field_validator("subject", "body")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        """避免将仅包含空白字符的主题或正文保存为草稿。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("主题和正文不能为空")
        return normalized
