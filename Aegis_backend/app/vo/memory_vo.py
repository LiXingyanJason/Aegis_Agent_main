"""长期记忆管理接口的返回模型。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class MemoryVO(BaseModel):
    """一条用户可管理的长期记忆。"""

    memory_id: UUID
    content: str
    source: str
    source_run_id: UUID | None
    is_sensitive: bool
    created_at: datetime
    updated_at: datetime


class MemoryListItemVO(BaseModel):
    """列表项目；敏感正文被脱敏，需通过详情接口查看。"""

    memory_id: UUID
    content_preview: str
    source: str
    is_sensitive: bool
    updated_at: datetime


class MemoryListVO(BaseModel):
    """记忆搜索结果与下一页游标。"""

    items: list[MemoryListItemVO]
    next_cursor: UUID | None = None


class MemoryDeletionRequestVO(BaseModel):
    """一次短期有效的删除确认凭据。"""

    memory_id: UUID
    deletion_token: str
    expires_at: datetime


class MemoryDeletionVO(BaseModel):
    """软删除完成后的状态响应。"""

    memory_id: UUID
    status: str = "deleted"
