from pathlib import Path
import sys
import pytest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from evaluate_rerank_ablation import snapshot


def test_frozen_real_candidates_are_reproducible_and_not_final_top_five():
    a,b=snapshot(),snapshot()
    assert a==b and len(a['rows'])==54
    assert sum(r['split']=='test' and r['kind']=='answerable' for r in a['rows'])==36
    assert all(len(r['candidate_keys'])==len(r['documents'])<=20 for r in a['rows'])
    assert any(len(r['documents'])>5 for r in a['rows'])
    assert all(set(r['reference_rankings'])=={'lexical','vector','rrf'} for r in a['rows'])
    assert all(k.startswith('D') or k=='C06.current' for r in a['rows'] for k in r['candidate_keys'])
