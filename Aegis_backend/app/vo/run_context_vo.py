"""面向用户的调用级记忆元数据；不包含正文、摘要、Prompt。"""
from uuid import UUID
from datetime import datetime
from typing import Literal
from pydantic import BaseModel,Field


class MemoryUseVO(BaseModel):
    memory_id: UUID
    memory_version: int | None = None
    memory_type: str | None = None
    source_kind: str | None = None
    source_object_id: UUID | None = None
    source_version: str | None = None
    included_in_prompt: bool
    selection_reason: str | None = None
    current_status: Literal['available','changed','inactive','restricted','deleted','unknown']


class MemoryAttemptVO(BaseModel):
    attempt_id: UUID
    purpose: str | None = None
    status: str
    started_at: datetime
    memory_count: int | None = None
    memory_policy_version: str | None = None
    selection_reason: str | None = None
    ranking_policy_version: str | None = None
    recall_truncated: bool = False
    excluded_counts: dict[str,int] = Field(default_factory=dict)
    needs_review: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    memories: list[MemoryUseVO] = Field(default_factory=list)


class RunContextSummaryVO(BaseModel):
    run_id: UUID
    attempts: list[MemoryAttemptVO]
    legacy_memories: list[MemoryUseVO]
    truncated: bool = False
