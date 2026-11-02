from hashlib import sha256
from uuid import uuid4
import pytest
from pydantic import ValidationError
from aegis_agent_worker.indexing.contracts import (
    RawChunk, SourceScope, SourceRange, RetrievalText, EvidenceOrigin, EvidenceContext, merge_ranges,
)


def chunk():
    return RawChunk(scope=SourceScope(document_id=uuid4(), version_id=uuid4(), generation_id=uuid4(),
        parser_version='structure-v1'), structure='clause', heading_path=('制度', '例外'),
        clause_id='clause-1', source_range=SourceRange(start=0,end=3), content='甲乙丙',
        content_hash=sha256('甲乙丙'.encode()).hexdigest())


def test_raw_slice_hash_and_stable_generation_identity():
    c=chunk()
    assert c.verify_source('甲乙丙丁')==c
    assert RawChunk.model_validate_json(c.model_dump_json()).chunk_id==c.chunk_id
    with pytest.raises(ValueError): c.verify_source('丙乙甲丁')
    raw=c.model_dump(); raw['content_hash']='0'*64
    with pytest.raises(ValidationError): RawChunk(**raw)
    raw=c.model_dump(); raw['scope']['generation_id']=uuid4()
    assert RawChunk(**raw).chunk_id!=c.chunk_id


def test_title_enhancement_is_not_original_evidence():
    c=chunk()
    e=RetrievalText(chunk_id=c.chunk_id,document_title='标题',heading_path=c.heading_path,raw_content=c.content)
    assert e.verify_chunk(c)==e and e.input_hash!=c.content_hash
    with pytest.raises(ValueError):
        RetrievalText(chunk_id=uuid4(),document_title='标题',raw_content='甲乙丙').verify_chunk(c)


@pytest.mark.parametrize('kwargs', [
    dict(kind='direct'), dict(kind='adjacent',rrf_rank=1,trigger_chunk_id=uuid4()),dict(kind='same_clause')])
def test_origin_cannot_forge_direct_ranking(kwargs):
    with pytest.raises(ValidationError): EvidenceOrigin(chunk_id=uuid4(),**kwargs)


def test_union_preserves_gaps_and_multispan_context():
    spans=merge_ranges([SourceRange(start=2,end=5),SourceRange(start=0,end=3),SourceRange(start=7,end=9)])
    assert [(s.start,s.end) for s in spans]==[(0,5),(7,9)]
    c=chunk()
    ctx=EvidenceContext(scope=c.scope,ranges=spans,origins=(EvidenceOrigin(kind='direct',chunk_id=c.chunk_id,rrf_rank=1),))
    assert ctx.verify_origins({c.chunk_id:c})==ctx
    assert ctx.source_parts('甲乙丙丁戊己庚辛壬')==('甲乙丙丁戊','辛壬')
    other=chunk()
    with pytest.raises(ValueError): ctx.verify_origins({c.chunk_id:other})


def test_incomplete_and_non_normalized_ranges_rejected():
    c=chunk(); origin=EvidenceOrigin(kind='direct',chunk_id=c.chunk_id,rrf_rank=1)
    with pytest.raises(ValidationError):
        EvidenceContext(scope=c.scope,ranges=(c.source_range,),origins=(origin,),completeness='incomplete')
    with pytest.raises(ValidationError):
        EvidenceContext(scope=c.scope,ranges=(c.source_range,c.source_range),origins=(origin,))
