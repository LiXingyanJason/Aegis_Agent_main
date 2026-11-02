"""Phase 2 structural contracts; not wired to the production index or API.

Validation proves shape/content integrity, never tenant authorization or completeness
of business meaning. Offsets are Unicode characters into the saved canonical source.
"""
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid5
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class SourceScope(Contract):
    document_id: UUID
    version_id: UUID
    generation_id: UUID
    parser_version: str = Field(min_length=1, max_length=64)


class SourceRange(Contract):
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, gt=0)

    @model_validator(mode='after')
    def ordered(self):
        if self.end <= self.start:
            raise ValueError('empty or reversed source range')
        return self


class RawChunk(Contract):
    scope: SourceScope
    structure: Literal['paragraph', 'clause', 'list', 'table', 'heading', 'hard_split']
    heading_path: tuple[str, ...] = Field(default=(), max_length=6)
    clause_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_range: SourceRange
    content: str = Field(min_length=1, max_length=800)
    content_hash: str = Field(pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def integrity(self):
        if self.source_range.end-self.source_range.start != len(self.content):
            raise ValueError('range/content length mismatch')
        if sha256(self.content.encode('utf-8')).hexdigest() != self.content_hash:
            raise ValueError('raw content hash mismatch')
        if any(not value.strip() or len(value)>200 for value in self.heading_path):
            raise ValueError('invalid heading path')
        return self

    def verify_source(self, source: str):
        r = self.source_range
        if source[r.start:r.end] != self.content:
            raise ValueError('content is not the original slice')
        return self

    @property
    def chunk_id(self):
        identity = self.model_dump_json()
        return uuid5(self.scope.generation_id, identity)


class RetrievalText(Contract):
    chunk_id: UUID
    enhancement_version: Literal['title-path-v1'] = 'title-path-v1'
    document_title: str = Field(min_length=1, max_length=200)
    heading_path: tuple[str, ...] = Field(default=(), max_length=6)
    raw_content: str = Field(min_length=1, max_length=800)

    @property
    def text(self):
        return '\n'.join((self.document_title, *self.heading_path, self.raw_content))

    @property
    def input_hash(self):
        return sha256(self.text.encode('utf-8')).hexdigest()

    def verify_chunk(self, chunk: RawChunk):
        if (self.chunk_id, self.heading_path, self.raw_content) != (chunk.chunk_id, chunk.heading_path, chunk.content):
            raise ValueError('enhancement does not match raw chunk')
        return self


class EvidenceOrigin(Contract):
    kind: Literal['direct', 'same_clause', 'adjacent', 'parent']
    chunk_id: UUID
    trigger_chunk_id: UUID | None = None
    rrf_rank: int | None = Field(default=None, strict=True, ge=1)

    @model_validator(mode='after')
    def provenance(self):
        if self.kind == 'direct':
            if self.trigger_chunk_id is not None or self.rrf_rank is None:
                raise ValueError('direct evidence requires its own rank only')
        elif self.trigger_chunk_id is None or self.rrf_rank is not None:
            raise ValueError('expansion requires trigger and cannot claim a direct rank')
        return self


class EvidenceContext(Contract):
    scope: SourceScope
    ranges: tuple[SourceRange, ...] = Field(min_length=1, max_length=16)
    origins: tuple[EvidenceOrigin, ...] = Field(min_length=1, max_length=32)
    completeness: Literal['not_assessed', 'scope_complete', 'incomplete'] = 'not_assessed'
    reason: Literal['budget', 'source_unavailable', 'unresolved_reference'] | None = None

    @model_validator(mode='after')
    def normalized(self):
        for left, right in zip(self.ranges, self.ranges[1:]):
            if left.end >= right.start:
                raise ValueError('ranges must be sorted and overlapping/adjacent ranges merged')
        if len({o.chunk_id for o in self.origins}) != len(self.origins):
            raise ValueError('duplicate origins; direct hit takes precedence')
        if (self.completeness == 'incomplete') != (self.reason is not None):
            raise ValueError('incomplete context requires a stable reason')
        return self

    def verify_origins(self, chunks: dict[UUID, RawChunk]):
        for origin in self.origins:
            chunk = chunks[origin.chunk_id]
            if chunk.scope != self.scope:
                raise ValueError('mixed source versions or index generations')
            r = chunk.source_range
            if not any(s.start <= r.start and s.end >= r.end for s in self.ranges):
                raise ValueError('origin range missing from context')
            if origin.trigger_chunk_id is not None:
                trigger = chunks[origin.trigger_chunk_id]
                if trigger.scope != self.scope:
                    raise ValueError('expansion trigger outside scope')
                if origin.kind=='same_clause' and (not chunk.clause_id or chunk.clause_id!=trigger.clause_id):
                    raise ValueError('different clause')
        return self

    def source_parts(self, source: str):
        if any(r.end>len(source) for r in self.ranges):
            raise ValueError('range outside source')
        return tuple(source[r.start:r.end] for r in self.ranges)


def merge_ranges(ranges):
    """Union only overlap/adjacency; never fill an unobserved gap."""
    result = []
    for r in sorted(ranges, key=lambda r: (r.start, r.end)):
        if result and r.start <= result[-1].end:
            result[-1] = SourceRange(start=result[-1].start, end=max(r.end, result[-1].end))
        else:
            result.append(r)
    return tuple(result)
