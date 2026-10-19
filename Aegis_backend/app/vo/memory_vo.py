"""长期记忆管理接口的返回模型。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel,computed_field,TypeAdapter,ValidationError
from app.param.memory_entity import MemoryEntity,describe_memory_entity


class MemoryMetadataVO(BaseModel):
    memory_type: str = 'long_term_summary'
    scope: str = 'user'
    entity_key: str | None = None
    status: str = 'pending'
    version: int = 1
    sensitivity: str = 'normal'
    confirmed_at: datetime | None = None
    expires_at: datetime | None = None
    source_kind: str = 'manual'
    source_object_id: UUID | None = None
    source_version: str | None = None
    supersedes_id: UUID | None = None
    source_available: bool = True
    prompt_eligible: bool = False


class MemoryVO(MemoryMetadataVO):
    """一条用户可管理的长期记忆。"""

    memory_id: UUID
    content: str
    source: str
    source_run_id: UUID | None
    is_sensitive: bool
    created_at: datetime
    updated_at: datetime
    entity: dict | None = None

    @computed_field
    @property
    def content_is_generated(self) -> bool:
        """按当前字段计算，而非新增存储标记；避免编辑时沿用旧值的自动说明。"""
        if self.memory_type!='entity' or self.entity is None:
            return False
        try:
            return self.content==describe_memory_entity(TypeAdapter(MemoryEntity).validate_python(self.entity))
        except ValidationError:
            return False


class MemoryListItemVO(MemoryMetadataVO):
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
