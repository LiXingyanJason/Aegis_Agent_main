"""评测工具本身的离线回归，不能证明真实模型分类正确率。"""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"scripts"))
import evaluate_main_routing as evaluation
from routing_eval.metrics import summarize


DATA = evaluation.load_cases(ROOT/"tests/evaluation/data/main_routing_cases.json")


def test_fixed_dataset_has_coverage_and_unique_ids():
    assert len(DATA["cases"]) >= 60
    assert {"semantic", "fault", "page"} == {c["scenario"] for c in DATA["cases"]}
    assert {"valid", "expired", "stale", "foreign", "missing"} <= {
        c.get("card") for c in DATA["cases"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", DATA["cases"], ids=lambda c: c["id"])
async def test_current_graph_with_explicit_mock_fixtures(case):
    args = SimpleNamespace(mode="mock", env_file=None)
    row = await evaluation.evaluate(case, args, [20], DATA["fixed_now"])
    assert row["passed"], row
    assert row["external_business_calls"] == 0 and row["illegal_calls"] == []
    if case["scenario"] == "page":
        assert not row["calls"]
    if case["mock"] == "repair":
        assert len(row["calls"]) == 2 and row["repair_count"] == 1


def test_unknown_usage_and_mock_are_not_real_accuracy():
    summary = summarize([], "mock")
    assert summary["semantic_routing_accuracy"] is None
    assert summary["usage"]["total_tokens"]["reported_sum"] is None
    assert summary["ready_for_release"] is False


def test_report_comparison_rejects_different_conditions():
    from compare_routing_reports import compare
    before = dict(dataset_sha256="one", fixed_now="same", mode="mock")
    with pytest.raises(ValueError, match="dataset_sha256"):
        compare(before, {**before, "dataset_sha256": "two"})
    with pytest.raises(ValueError, match="不完整"):
        compare({**before, "complete": False}, {**before, "complete": True})


def test_source_revision_verification_accepts_only_matching_python(tmp_path, monkeypatch):
    import hashlib
    path = tmp_path/"src/aegis_agent_worker/example.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x = 1\r\n")
    content = b"x = 1\n"
    blob = hashlib.sha1(b"blob "+str(len(content)).encode()+b"\0"+content).hexdigest()
    def output(command, **kwargs):
        if "rev-parse" in command:
            return "verified-commit\n"
        return f"100644 blob {blob}\taegis_agent_worker/src/aegis_agent_worker/example.py\n"
    monkeypatch.setattr(evaluation.subprocess, "check_output", output)
    assert evaluation.verify_source_revision(tmp_path, "baseline") == "verified-commit"
    path.write_bytes(b"x = 2\n")
    with pytest.raises(ValueError, match="不匹配"):
        evaluation.verify_source_revision(tmp_path, "baseline")
