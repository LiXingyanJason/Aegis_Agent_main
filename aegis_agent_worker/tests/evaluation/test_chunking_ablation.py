import sys
from pathlib import Path
from copy import deepcopy
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from evaluate_chunking_ablation import run, metrics, normalize, paired_interval


def test_range_metrics_do_not_credit_wrong_document_or_hide_missing_exception():
    q=dict(evidence=[dict(document_id='a',start=0,end=5,role='rule'),dict(document_id='a',start=10,end=15,role='exception')])
    m=metrics(q,[dict(document_id='a',start_offset=0,end_offset=5)])
    assert m==dict(span_coverage=.5,all_required=0,qualifiers=0)
    assert metrics(q,[dict(document_id='b',start_offset=0,end_offset=50)])['span_coverage']==0
    assert metrics(dict(evidence=[]),[])['span_coverage'] is None


def test_normalization_unions_only_same_document_and_no_gaps():
    rows=[dict(document_id='a',start_offset=0,end_offset=3),dict(document_id='a',start_offset=2,end_offset=5),
          dict(document_id='a',start_offset=7,end_offset=9),dict(document_id='b',start_offset=0,end_offset=9)]
    assert len(normalize(rows))==3


def test_factorial_matrix_same_budgets_negative_results_preserved():
    report=run()
    assert len(report['traces'])==136 and len(report['summary']['test'])==8
    for r in report['traces']:
        assert len(r['ranges'])<=5 and r['delivered_chars']<=3500 and r['result_bytes']<=12000
        assert r['answer'] is None and r['provider_usage'] is None
    assert report['external_calls']==0
    assert all(s['n']==10 for s in report['summary']['test'].values())
    assert report['failures']  # Do not silently discard regressions or budget failures.
    for arm in report['paired']:
        assert paired_interval(report['traces'],arm,'paragraph-title0-expand0','span_coverage')==report['paired'][arm]


def test_reproducible_rankings_exclude_wall_clock():
    a,b=run(),run()
    for report in (a,b):
        for r in report['traces']: r.pop('retrieval_ms')
    assert a['traces']==b['traces']
