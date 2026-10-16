"""不包含原文、存储key、密钥或用户身份的页面索引状态。"""
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel


class KnowledgeImportVO(BaseModel):
    document_id: UUID
    version_id: UUID
    job_id: UUID | None = None
    status: str
    index_status: str = "not_indexed"
    enabled: bool = False
    idempotent_replay: bool = False


class KnowledgeDocumentVO(BaseModel):
    document_id: UUID
    title: str
    visibility: str
    version_id: UUID
    job_id: UUID | None = None
    index_status: str
    enabled: bool
    is_sensitive: bool
    external_embedding_allowed: bool
    can_manage: bool
    created_at: datetime


class KnowledgeActivationVO(BaseModel):
    document_id: UUID
    enabled: bool
    index_status: str


class KnowledgeDocumentDetailVO(KnowledgeDocumentVO):
    version_label: str
    indexed_chunks: int


class KnowledgeChunkVO(BaseModel):
    document_id: UUID
    version_id: UUID
    chunk_id: UUID
    title: str
    version_label: str
    section: str
    start_offset: int
    end_offset: int
    excerpt: str
    content_hash: str


class KnowledgeIndexJobVO(BaseModel):
    job_id: UUID
    document_id: UUID
    version_id: UUID
    status: str
    attempt_count: int
    total_chunks: int
    processed_chunks: int
    error_code: str | None
    updated_at: datetime
