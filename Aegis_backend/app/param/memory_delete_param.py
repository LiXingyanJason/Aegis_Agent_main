"""确认软删除长期记忆的请求参数。"""

from pydantic import BaseModel, Field


class MemoryDeleteParam(BaseModel):
    """删除令牌只由删除申请接口签发，数据库仅保存其哈希。"""

    deletion_token: str = Field(min_length=20, max_length=512)
