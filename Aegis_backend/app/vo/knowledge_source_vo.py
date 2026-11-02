"""知识来源卡片快照校验；主后端不依赖Worker包。"""
from uuid import UUID
from typing import Literal
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


class KnowledgeSourcePart(BaseModel):
    model_config=ConfigDict(extra='forbid')
    chunk_id: UUID
    start_offset: int = Field(strict=True,ge=0)
    end_offset: int = Field(strict=True,gt=0)
    excerpt: str = Field(min_length=1,max_length=800)
    content_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    origin: Literal['same_clause','adjacent','related']
    trigger_chunk_id: UUID


class KnowledgeSourceV2(KnowledgeSourceSnapshot):
    generation_id: UUID | None = None
    parts: list[KnowledgeSourcePart] = Field(default_factory=list,max_length=2)
    context_incomplete: bool = False


class KnowledgeSourcesV2Payload(BaseModel):
    model_config=ConfigDict(extra='forbid')
    schema_version: Literal[2]
    sources: list[KnowledgeSourceV2] = Field(min_length=1,max_length=5)
    has_conflict: bool = Field(default=False,strict=True)

    @model_validator(mode='after')
    def distinct(self):
        if len({s.chunk_id for s in self.sources})!=len(self.sources) or len({s.source_no for s in self.sources})!=len(self.sources):
            raise ValueError('来源引用重复')
        for source in self.sources:
            if source.parts and (source.generation_id is None or any(p.trigger_chunk_id!=source.chunk_id for p in source.parts)):
                raise ValueError('复合来源代次或触发关系错误')
        return self
