"""实验统计口径：重复题聚类、未知Token、Mock不算语义准确率、JUnit不冒充模型质量。"""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"scripts"))
from summarize_resume_experiments import archive_evidence, clustered_interval, percentile, summarize_routing, usage, junit_summary, category


def row(case="case01", repetition=1, passed=True, live=True):
    return dict(case_id=case, scenario="semantic", entry="conversation", repetition=repetition,
        passed=passed, actual="reply" if passed else "clarify", target_ok=True, live_model=live,
        decision="reply" if passed else "clarify", repair_count=0, candidate_recall=None,
        duration_ms=10*repetition, calls=[dict(prompt_tokens=10, completion_tokens=None, total_tokens=None)],
        illegal_calls=[], outcome=None, error=None)


def test_percentile_uses_nearest_rank_and_empty_unknown():
    assert percentile([], .95) is None
    assert percentile(list(range(1, 21)), .95) == 19


def test_supplier_unknown_usage_is_not_zero():
    result = usage([dict(prompt_tokens=None), dict(prompt_tokens=7)])
    assert result["prompt_tokens"] == dict(reported_calls=1, unknown_calls=1, reported_sum=7)
    assert result["completion_tokens"]["reported_sum"] is None
    assert usage([])["total_tokens"]["reported_sum"] is None


def test_repeated_cases_are_counted_separately_from_unique_questions():
    rows = [row("case01", n) for n in range(1, 4)] + [row("case02", n, n > 1) for n in range(1, 4)]
    report = dict(mode="live", complete=True, results=rows)
    dataset = dict(cases=[dict(id=key, scenario="semantic") for key in ("case01", "case02")])
    summary = summarize_routing(report, dataset)
    assert summary["runs"] == 6 and summary["unique_cases"] == 2
    assert summary["correct"] == 5 and summary["stable_route_and_target_cases"] == 1
    assert summary["all_repetitions_correct_cases"] == 1 and summary["per_case_majority_correct"] == 2
    assert summary["cluster_confidence_interval"]["method"] == "case_cluster_percentile_bootstrap"


def test_cluster_bootstrap_is_deterministic_and_keeps_whole_questions():
    rows = [row("one", n) for n in (1, 2, 3)] + [row("two", n, False) for n in (1, 2, 3)]
    first = clustered_interval(rows, draws=200)
    assert first == clustered_interval(rows, draws=200)
    assert first["lower"] == 0 and first["upper"] == 1


def test_mock_never_claims_real_accuracy_or_bootstrap():
    summary = summarize_routing(dict(mode="mock", complete=True, results=[row(live=False)]),
                                dict(cases=[dict(id="case01", scenario="semantic")]))
    assert summary["accuracy"] is None and summary["cluster_confidence_interval"] is None
    assert summary["real_model_calls"] == 0 and summary["illegal_business_tool_rate"] is None


def test_unbalanced_or_incomplete_report_has_no_confidence_interval():
    rows = [row("one"), row("two"), row("two", 2)]
    summary = summarize_routing(dict(mode="live", complete=False, results=rows),
        dict(cases=[dict(id=key, scenario="semantic") for key in ("one", "two")]))
    assert not summary["balanced_repetitions"]
    assert summary["cluster_confidence_interval"] is None


def test_junit_skips_failures_and_file_categories_are_not_model_accuracy(tmp_path):
    xml = tmp_path/"junit.xml"
    xml.write_text('<testsuites><testsuite><testcase classname="tests.agent.test_harness_layers" name="a"/>'
        '<testcase classname="tests.memory.test_session_memory_live" name="b"><skipped/></testcase>'
        '<testcase classname="tests.evaluation.test_routing_evaluation" name="c"><failure/></testcase>'
        '</testsuite></testsuites>', encoding="utf-8")
    result = junit_summary(xml)
    assert result["total"] == 3 and result["passed"] == result["failed"] == result["skipped"] == 1
    assert result["governance_related"]["passed"] == 1
    assert category("tests.knowledge.test_hybrid_retrieval") == "knowledge"


def test_explicit_evidence_archive_refuses_overwrite(tmp_path):
    original = tmp_path/"source.json"
    original.write_text('{"safe":true}', encoding="utf-8")
    archive = tmp_path/"evidence"
    result = archive_evidence([original], archive)
    assert result[0]["bytes"] == original.stat().st_size
    assert (archive/original.name).read_bytes() == original.read_bytes()
    with pytest.raises(ValueError, match="不覆盖"):
        archive_evidence([original], archive)


def test_archive_refuses_same_name_from_two_directories(tmp_path):
    with pytest.raises(ValueError, match="不能重复"):
        archive_evidence([tmp_path/"one/result.json", tmp_path/"two/result.json"], tmp_path/"evidence")
