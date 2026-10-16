"""知识来源卡片快照校验；主后端不依赖Worker包。"""
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator


class KnowledgeSourceSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_no: int = Field(strict=True,ge=1,le=5)
    document_id: UUID
    version_id: UUID
    chunk_id: UUID
    title: str = Field(min_length=1,max_length=200)
    version_label: str = Field(min_length=1,max_length=64)
    section: str = Field(min_length=1,max_length=200)
    start_offset: int = Field(strict=True,ge=0)
    end_offset: int = Field(strict=True,gt=0)
    excerpt: str = Field(min_length=1,max_length=800)
    content_hash: str = Field(pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def position(self):
        if self.end_offset<=self.start_offset or not self.excerpt.strip():
            raise ValueError('来源位置无效')
        return self


class KnowledgeSourceSnapshotPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sources: list[KnowledgeSourceSnapshot] = Field(min_length=1,max_length=5)
    has_conflict: bool = Field(default=False,strict=True)

    @model_validator(mode='after')
    def distinct(self):
        if (len({item.chunk_id for item in self.sources})!=len(self.sources)
            or len({item.source_no for item in self.sources})!=len(self.sources)):
            raise ValueError('来源引用重复')
        return self
