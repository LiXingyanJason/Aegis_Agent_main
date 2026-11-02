"""Immutable shadow rebuild manifest. No vector writes or production publication."""
from hashlib import sha256
import json
from uuid import UUID
from pydantic import Field
from .contracts import Contract, SourceScope
from .structural import parse_structure, StructuralPolicy


def digest(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class GenerationConfig(Contract):
    version: str = Field(default='shadow-structure-v1',pattern=r'^shadow-structure-v1$')
    parser_version: str = Field(default='structure-v1',pattern=r'^structure-v1$')
    size: int = Field(default=700,strict=True,ge=1,le=800)
    overlap: int = Field(default=80,strict=True,ge=0,le=799)
    title_enhancement: bool = Field(default=False,strict=True)

    def policy(self):
        return StructuralPolicy(size=self.size,overlap=self.overlap)


def build_manifest(document_id: UUID,version_id: UUID,generation_id: UUID,raw: bytes,title: str,config: GenerationConfig):
    # Source byte hash and canonical text hash remain separate (UTF-8 BOM included
    # in original bytes, removed only for canonical decoding as production does).
    policy=config.policy()
    if not raw or len(raw)>262144: raise ValueError('source byte limit')
    source=raw.decode('utf-8-sig')
    scope=SourceScope(document_id=document_id,version_id=version_id,generation_id=generation_id,
                      parser_version=config.parser_version)
    parsed=parse_structure(source,scope,policy)
    chunks=[]
    enhancements={e.chunk_id:e for e in parsed.enhanced(title)} if config.title_enhancement else {}
    for position,c in enumerate(parsed.chunks):
        enhanced=enhancements.get(c.chunk_id)
        item=dict(position=position,chunk_id=str(c.chunk_id),raw=c.model_dump(mode='json'),
            relations=[r.model_dump() for r in parsed.relations[c.chunk_id]],
            retrieval_text=enhanced.text if enhanced else c.content,
            retrieval_hash=enhanced.input_hash if enhanced else c.content_hash)
        chunks.append(item)
    body=dict(scope=scope.model_dump(mode='json'),source_hash=sha256(raw).hexdigest(),
        canonical_hash=sha256(source.encode()).hexdigest(),config=config.model_dump(),chunks=chunks)
    return dict(**body,manifest_hash=digest(body))
