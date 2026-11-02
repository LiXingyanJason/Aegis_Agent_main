from uuid import uuid4
import pytest
from aegis_agent_worker.indexing.contracts import SourceScope
from aegis_agent_worker.indexing.structural import parse_structure, StructuralPolicy, PARSER_VERSION
from aegis_agent_worker.retrieval.expansion import expand_evidence, ExpansionPolicy


def scope():
    return SourceScope(document_id=uuid4(),version_id=uuid4(),generation_id=uuid4(),parser_version=PARSER_VERSION)


def test_structure_paths_lists_tables_and_raw_slices():
    text='# 制度\n## 借用\n申请条件如下：\n\n- 设备A登记\n  - 配件核对\n- 设备B审批\n\n|设备|限制|\n|---|---|\n|A|不得转借|\n\n## 例外\n普通说明。'
    result=parse_structure(text,scope(),StructuralPolicy(size=70,overlap=10))
    assert {'list','table','heading','paragraph'} <= {c.structure for c in result.chunks}
    assert any(c.heading_path==('制度','借用') for c in result.chunks)
    assert all(c.verify_source(text) for c in result.chunks)
    assert all(len(c.content)<=70 for c in result.chunks)
    assert any(result.relations[c.chunk_id] for c in result.chunks if c.structure=='list')
    assert any(result.relations[c.chunk_id] for c in result.chunks if c.structure=='table')
    assert len(result.enhanced('资料标题'))==len(result.chunks)


def test_long_clause_hard_cut_is_bounded_and_stable():
    text='# 条款\n1.1 '+ '甲'*180+'。\n后续条件。'
    s=scope(); p=StructuralPolicy(size=40,overlap=8)
    a=parse_structure(text,s,p); b=parse_structure(text,s,p)
    assert a.chunks==b.chunks
    clauses=[c for c in a.chunks if c.clause_id]
    assert len({c.clause_id for c in clauses})==1
    assert any(c.structure=='hard_split' for c in clauses)
    assert max(len(c.content) for c in a.chunks)<=40
    with pytest.raises(ValueError): parse_structure(text,s,StructuralPolicy(size=20,maximum=2))


def test_expansion_same_clause_priority_budget_and_no_fake_ranking():
    text='# 制度\n1.1 '+'要求。\n'*15+'\n例外说明。\n\n## 其他\n不得扩展章节。'
    s=scope(); result=parse_structure(text,s,StructuralPolicy(size=40,overlap=0))
    direct=next(c for c in result.chunks if c.clause_id)
    expanded=expand_evidence([direct.chunk_id],result.chunks,{s:text},
        policy=ExpansionPolicy(max_expansions=1,max_chars=200),relations=result.relations)
    assert expanded.incomplete
    origins=[o for ctx in expanded.contexts for o in ctx.origins]
    assert origins[0].kind=='direct' and origins[0].rrf_rank==1
    assert any(o.kind=='same_clause' and o.rrf_rank is None for o in origins)
    assert expanded.chars<=200
    assert all(ctx.scope==s for ctx in expanded.contexts)


def test_generation_isolation_missing_and_duplicate_hits():
    s=scope(); t=scope(); text='甲。\n\n乙。'
    a=parse_structure(text,s); b=parse_structure(text,t)
    cid=a.chunks[0].chunk_id
    r=expand_evidence([cid],[*a.chunks,*b.chunks],{s:text,t:text})
    assert len(r.contexts)==1 and r.contexts[0].scope==s
    assert expand_evidence([uuid4()],a.chunks,{s:text}).incomplete
    with pytest.raises(ValueError): expand_evidence([cid,cid],a.chunks,{s:text})
    with pytest.raises(ValueError): expand_evidence([cid],a.chunks,{s:'错误原文'})


def test_oversized_direct_is_incomplete_not_truncated():
    s=scope(); text='甲'*90
    a=parse_structure(text,s)
    r=expand_evidence([a.chunks[0].chunk_id],a.chunks,{s:text},policy=ExpansionPolicy(max_chars=10))
    assert r.incomplete and not r.contexts and r.chars==0


def test_code_fence_does_not_change_heading_path():
    text='# 真标题\n```markdown\n# 代码内伪标题\n```\n正文。'
    a=parse_structure(text,scope())
    assert all(c.heading_path==('真标题',) for c in a.chunks)


def test_long_table_references_header_without_copying_it():
    text='# 表\n| A | B |\n| --- | --- |\n'+''.join(f'| 条目{i} | 内容{i} |\n' for i in range(20))
    a=parse_structure(text,scope(),StructuralPolicy(size=55,overlap=0))
    tables=[c for c in a.chunks if c.structure=='table']
    assert len(tables)>2 and all(a.relations[c.chunk_id] for c in tables)
    assert all(c.verify_source(text) for c in tables)


@pytest.mark.parametrize('kwargs',[dict(size=801),dict(overlap=700),dict(maximum=0)])
def test_invalid_policy(kwargs):
    with pytest.raises(ValueError): StructuralPolicy(**kwargs)
