"""E04样本冻结、相关性分母、配对统计和实验隔离检查。"""
from pathlib import Path
from types import SimpleNamespace
import sys
import math
from copy import deepcopy

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"scripts"))
from retrieval_eval.dataset import load_dataset, identifier
from retrieval_eval.metrics import ranking_metrics, paired_intervals, summarize, excludes_target
from evaluate_hybrid_retrieval import IsolatedDatabase
from summarize_hybrid_retrieval import check_reports


def test_frozen_dataset_has_real_questions_and_separate_denominators():
    data, queries, chunks = load_dataset(ROOT/"tests/evaluation/data/hybrid_retrieval_e04_v1.json")
    assert len(data["documents"])+len(data["canaries"]) == 30
    assert len(chunks) == len({item["key"] for item in chunks}) == 79
    assert len(queries) == len({item["id"] for item in queries}) == 60
    assert sum(q["kind"] == "answerable" and q["split"] == "dev" for q in queries) == 12
    assert sum(q["kind"] == "answerable" and q["split"] == "test" for q in queries) == 36
    assert sum(q["kind"] == "no_answer" for q in queries) == 6
    assert sum(q["kind"] == "acl" for q in queries) == 6
    ids = {chunk["key"] for chunk in chunks}
    assert all(set(query["qrels"]) <= ids for query in queries)
    assert identifier("D01.rule") == identifier("D01.rule") != identifier("D02.rule")
    assert all(len(chunk["content"]) <= 800 for chunk in chunks)


def test_graded_metrics_and_first_relevant_rank():
    result = ranking_metrics(["x", "b", "a"], {"a": 2, "b": 1})
    assert result["recall"] == result["hit"] == 1
    assert result["mrr"] == .5
    assert result["ndcg"] == pytest.approx((1/math.log2(3)+3/math.log2(4))/(3+1/math.log2(3)))
    assert ranking_metrics([], {"a": 2}) == dict(recall=0, hit=0, mrr=0, ndcg=0)


@pytest.mark.parametrize("ranking,qrels", [(["a", "a"], {"a": 2}), (["a"], {}), (["a"], {"a": 0})])
def test_no_answer_and_duplicate_rankings_are_not_scored_as_answerable(ranking, qrels):
    with pytest.raises(ValueError): ranking_metrics(ranking, qrels)


def test_test_metrics_ignore_dev_and_no_answer():
    rows = [dict(kind="answerable", split="test", metrics={arm: ranking_metrics(["a"], {"a": 2})
            for arm in ("lexical", "vector", "rrf")}),
            dict(kind="no_answer", split="test", metrics={}), dict(kind="answerable", split="dev", metrics={})]
    assert summarize(rows, "test")["rrf"]["recall"] == 1


def test_paired_bootstrap_keeps_document_groups_and_reports_pp_not_relative_percent():
    rows = []
    for doc in ("one", "two"):
        for _ in range(2):
            rows.append(dict(kind="answerable", split="test", document_id=doc,
                metrics={arm: dict(recall=value, hit=value, mrr=value, ndcg=value)
                    for arm, value in (("lexical", .5), ("vector", 1), ("rrf", 1))}))
    result = paired_intervals(rows, draws=100)
    assert result == paired_intervals(rows, draws=100)
    assert result["groups"] == 2
    recall = result["comparisons"]["lexical"]["recall"]
    assert recall["delta_percentage_points"] == 50 and recall["relative_change"] == 1
    assert recall["ci95"] == [.5, .5]
    assert paired_intervals([]) == {}


@pytest.mark.parametrize("name", ["public", "aegis_knowledge_v1", "aegis_e04_abc; DROP TABLE users", ""])
def test_database_schema_cannot_target_business_or_accept_sql(name):
    with pytest.raises(ValueError): IsolatedDatabase(SimpleNamespace(), name)


def test_chunk_invalidity_is_not_document_revocation():
    doc, bad, good = identifier("D07"), identifier("D07.rule"), identifier("D07.exception")
    evidence = [SimpleNamespace(document_id=doc, chunk_id=good)]
    assert excludes_target(evidence, doc, bad)
    assert not excludes_target(evidence, doc)
    assert not excludes_target([SimpleNamespace(document_id=doc, chunk_id=bad)], doc, bad)


def test_verification_cannot_silently_change_old_ranks_labels_or_parameters():
    original = {key: {} for key in ("dataset_sha256", "experiment_id", "results", "test", "dev",
                                   "paired_bootstrap", "no_answer", "policy", "source_sha256")}
    verified = {**deepcopy(original), "complete":True, "cleanup":True, "verification":{"passed":True}}
    check_reports(original, verified)
    verified["results"] = ["changed"]
    with pytest.raises(ValueError,match="原始排名"):
        check_reports(original, verified)
