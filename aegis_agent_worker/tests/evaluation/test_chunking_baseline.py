import sys
from pathlib import Path
from copy import deepcopy
import json
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
from evaluate_chunking_baseline import freeze, run, covered, DATA


def test_freeze_original_ranges_and_no_topic_leakage():
    _, docs, queries, _ = freeze()
    assert len(docs)==8 and len(queries)==17
    assert all(len(d['text'])>700 for d in docs.values())
    for q in queries:
        for s in q['evidence']:
            assert docs[s['document_id']]['text'][s['start']:s['end']]==s['quote']
            assert docs[s['document_id']]['split']==q['split']


def test_reproducible_chunks_and_traces_without_network():
    a,b=run(),run()
    assert a['chunks']==b['chunks'] and a['queries']==b['queries']
    assert a['configuration']['external_calls']==0
    assert a['summary']['test']['no_answer']==1
    for c in a['chunks']:
        d=next(d for d in a['documents'] if d['id']==c['document_id'])
        assert d['text'][c['start_offset']:c['end_offset']]==c['content']
    assert all(q['answer'] is None and q['provider_usage'] is None for q in a['queries'])


def test_union_coverage_does_not_skip_gaps_or_other_documents():
    s=dict(document_id='a',start=0,end=10)
    assert covered(s,[dict(document_id='a',start_offset=0,end_offset=6),dict(document_id='a',start_offset=6,end_offset=10)])
    assert not covered(s,[dict(document_id='a',start_offset=0,end_offset=4),dict(document_id='a',start_offset=5,end_offset=10)])
    assert not covered(s,[dict(document_id='b',start_offset=0,end_offset=10)])


def test_split_leakage_rejected(tmp_path):
    data=json.loads(DATA.read_text(encoding='utf-8'))
    data['queries'][0]['split']='test'
    path=tmp_path/'invalid.json'
    path.write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises(ValueError): freeze(path)
