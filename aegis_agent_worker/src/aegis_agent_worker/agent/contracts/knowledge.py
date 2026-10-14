"""知识查询契约：模型只提出条件，身份／权限／有效版本来自代码事实。"""
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from aegis_agent_worker.agent.contracts.common import ConversationArtifact, ensure_json_object_size
from aegis_agent_worker.tool.contracts import ToolInvocation


class KnowledgeFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    category: Literal["project", "policy", "contract", "minutes"] | None = None


class KnowledgeQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    query: str = Field(min_length=1, max_length=1000)
    query_rewrites: list[str] = Field(default_factory=list, max_length=2)
    top_k: int = Field(default=5, strict=True, ge=1, le=5)
    filters: KnowledgeFilters = Field(default_factory=KnowledgeFilters)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value):
        if not value.strip():
            raise ValueError("查询不能为空")
        return value.strip()

    @field_validator("query_rewrites")
    @classmethod
    def validate_rewrites(cls, values):
        if any(not value.strip() or len(value) > 500 for value in values):
            raise ValueError("查询改写无效")
        normalized = [value.strip() for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("查询改写重复")
        return normalized

    def to_invocation(self):
        return ToolInvocation("knowledge.search", self.model_dump(mode="json"))


class RetrievedEvidence(BaseModel):
    """有界安全片段，不携带全文、向量、租户／用户ACL或可执行URL。"""
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_no: int = Field(strict=True, ge=1, le=5)
    document_id: UUID
    version_id: UUID
    chunk_id: UUID
    title: str = Field(min_length=1, max_length=200)
    version_label: str = Field(min_length=1, max_length=64)
    section: str = Field(min_length=1, max_length=200)
    start_offset: int = Field(strict=True, ge=0)
    end_offset: int = Field(strict=True, gt=0)
    excerpt: str = Field(min_length=1, max_length=800)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_position(self):
        if self.end_offset <= self.start_offset or not self.excerpt.strip():
            raise ValueError("证据位置或摘录无效")
        return self


class KnowledgeSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence: list[RetrievedEvidence] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def unique_sources(self):
        if (len({item.source_no for item in self.evidence}) != len(self.evidence)
                or len({item.chunk_id for item in self.evidence}) != len(self.evidence)):
            raise ValueError("证据编号或片段重复")
        ensure_json_object_size(self.model_dump(mode="json"), field_name="知识证据")
        return self


class KnowledgeAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    answer: str = Field(min_length=1, max_length=4000)
    citation_numbers: list[StrictInt] = Field(default_factory=list, max_length=5)
    can_confirm: bool = Field(strict=True)

    @field_validator("answer")
    @classmethod
    def nonempty_answer(cls, value):
        if not value.strip():
            raise ValueError("答案不能为空")
        return value.strip()

    @model_validator(mode="after")
    def validate_citations(self):
        values = self.citation_numbers
        if any(type(value) is not int or not 1 <= value <= 5 for value in values) or len(set(values)) != len(values):
            raise ValueError("引用编号无效")
        displayed = {int(value) for value in re.findall(r"\[(\d+)\]", self.answer)}
        if displayed != set(values) or (self.can_confirm and not values) or (not self.can_confirm and values):
            raise ValueError("答案和引用不一致")
        return self

    def verify_sources(self, evidence):
        if not set(self.citation_numbers) <= {item.source_no for item in evidence}:
            raise ValueError("引用不属于本次已核验证据")
        return self


class KnowledgeSourcesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sources: list[RetrievedEvidence] = Field(min_length=1, max_length=5)


class KnowledgeSourceArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    artifact_type: Literal["knowledge_sources"] = "knowledge_sources"
    payload: KnowledgeSourcesPayload

    def to_artifact(self):
        return ConversationArtifact.model_validate(self.model_dump(mode="json"))
