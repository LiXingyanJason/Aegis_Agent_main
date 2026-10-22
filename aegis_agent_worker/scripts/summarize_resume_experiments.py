"""汇总路由重复实验/JUnit；不调用模型，不把Mock或重复题当独立语义样本。"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import random
import platform
from statistics import mean
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


GOVERNANCE_MODULES = frozenset({"test_cooperation_governance", "test_harness_layers",
    "test_harness_react_revision", "test_main_card_react", "test_input_choices",
    "test_approval_controller", "test_memory_priority", "test_manual_memory_read_guard",
    "test_hybrid_retrieval"})


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_digest(root):
    source = Path(root)/"src"
    return hashlib.sha256(b"".join(path.relative_to(source).as_posix().encode()+path.read_bytes()
                                  for path in sorted(source.rglob("*.py")))).hexdigest()


def archive_evidence(paths, directory):
    """只归档显式指定的实验产物；拒绝覆盖和重名，不扫描环境/业务目录。"""
    directory = Path(directory)
    if len({Path(path).name for path in paths}) != len(paths):
        raise ValueError("归档证据文件名不能重复")
    if any((directory/Path(path).name).exists() for path in paths):
        raise ValueError("归档目标已存在，不覆盖历史实验")
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    for path in paths:
        path = Path(path)
        target = directory/path.name
        target.write_bytes(path.read_bytes())
        records.append(dict(path=str(target), sha256=digest(target), bytes=target.stat().st_size))
    return records


def percentile(values, q):
    values = sorted(values)
    return values[max(0, math.ceil(len(values)*q)-1)] if values else None


def clustered_interval(rows, seed=20261022, draws=10000):
    groups = defaultdict(list)
    for row in rows:
        groups[row["case_id"]].append(int(row["passed"]))
    clusters = list(groups.values())
    if not clusters:
        return None
    generator = random.Random(seed)
    means = []
    for _ in range(draws):
        selected = generator.choices(clusters, k=len(clusters))
        means.append(sum(sum(group) for group in selected)/sum(len(group) for group in selected))
    return dict(method="case_cluster_percentile_bootstrap", draws=draws, seed=seed,
        lower=percentile(means, .025), upper=percentile(means, .975),
        note="按固定题组重采样；不证明新问题/生产分布的泛化，也非159个独立问题")


def usage(calls):
    return {key: dict(reported_calls=sum(call.get(key) is not None for call in calls),
        unknown_calls=sum(call.get(key) is None for call in calls),
        reported_sum=sum(call[key] for call in calls if call.get(key) is not None)
        if any(call.get(key) is not None for call in calls) else None)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")}


def summarize_routing(report, dataset):
    rows = [row for row in report["results"] if row["scenario"] == "semantic" and row["entry"] == "conversation"]
    live = report["mode"] == "live" and all(row.get("live_model") for row in rows)
    grouped, rounds, categories = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in rows:
        grouped[row["case_id"]].append(row)
        rounds[row["repetition"]].append(row)
        prefix = row["case_id"].rstrip("0123456789")
        categories[prefix].append(row)
    def quality(items):
        return dict(correct=sum(row["passed"] for row in items), runs=len(items),
                    accuracy=mean(row["passed"] for row in items) if items and live else None)
    expected_ids = {case["id"] for case in dataset["cases"] if case["scenario"] == "semantic"}
    repeats = Counter(len(values) for values in grouped.values())
    uniform = len(repeats) == 1 and set(grouped) == expected_ids
    calls = [call for row in rows if row.get("live_model") for call in row["calls"]]
    repaired = [row for row in rows if row["repair_count"] > 0]
    recall = [row["candidate_recall"] for row in rows if row["candidate_recall"] is not None]
    safety_ids = {case["id"] for case in dataset["cases"] if case.get("forbid_delegation")}
    safety = [row for row in rows if row["case_id"] in safety_ids]
    return dict(scope="four_registered_capabilities_route_only_fixed_facts", live=live,
        report_complete=report["complete"], **quality(rows), unique_cases=len(grouped),
        balanced_repetitions=uniform, per_case_repetitions=dict(repeats),
        round_metrics={str(key): quality(value) for key, value in sorted(rounds.items())},
        category_metrics={key: quality(value) for key, value in sorted(categories.items())},
        cluster_confidence_interval=clustered_interval(rows) if live and uniform and report["complete"] else None,
        stable_route_and_target_cases=sum(len({(row["actual"], row["target_ok"]) for row in values}) == 1
                                          for values in grouped.values()) if uniform else None,
        all_repetitions_correct_cases=sum(all(row["passed"] for row in values) for values in grouped.values()) if uniform else None,
        per_case_majority_correct=sum(sum(row["passed"] for row in values) > len(values)/2
                                     for values in grouped.values()) if uniform else None,
        clarification_runs=sum(row["decision"] == "clarify" for row in rows),
        unnecessary_clarifications=sum(row["decision"] == "clarify" and not row["passed"] for row in rows),
        candidate_recall=dict(retained=sum(recall), eligible_observations=len(recall)),
        repair=dict(tasks=len(repaired), finally_correct=sum(row["passed"] for row in repaired)),
        latency_ms=dict(mean=mean(row["duration_ms"] for row in rows) if rows else None,
                        p50=percentile([row["duration_ms"] for row in rows], .5),
                        p95=percentile([row["duration_ms"] for row in rows], .95)),
        real_model_calls=len(calls), usage=usage(calls),
        prohibited_handoff=dict(eligible_observations=len(safety), violations=sum(bool(row["illegal_calls"]) for row in safety)),
        illegal_business_tool_rate=None,
        failures=[{key: row.get(key) for key in ("case_id", "repetition", "actual", "target_ok", "outcome", "error", "repair_count")}
                  for row in rows if not row["passed"]])


def category(classname):
    leaf = classname.rsplit(".", 1)[-1]
    if ".evaluation." in classname:
        return "routing_evaluation"
    if ".memory." in classname or leaf == "test_memory_context_service":
        return "memory"
    if ".knowledge." in classname:
        return "knowledge"
    if leaf.startswith("test_harness"):
        return "harness"
    if leaf.startswith("test_cooperation") or leaf in {"test_calendar_team_expert", "test_mail_meeting_requirements",
                                                     "test_meeting_attendees", "test_team_action_diagnostics"}:
        return "cooperation"
    return "other"


def junit_summary(path):
    root = ET.parse(path).getroot()
    records = []
    for test in root.iter("testcase"):
        status = ("failed" if test.find("failure") is not None or test.find("error") is not None else
                  "skipped" if test.find("skipped") is not None else "passed")
        records.append(dict(classname=test.get("classname", ""), name=test.get("name", ""), status=status,
                            seconds=float(test.get("time", 0))))
    def counts(items):
        counter = Counter(item["status"] for item in items)
        return dict(total=len(items), passed=counter["passed"], failed=counter["failed"], skipped=counter["skipped"])
    groups = defaultdict(list)
    for record in records:
        groups[category(record["classname"])].append(record)
    governance = [item for item in records if item["classname"].rsplit(".", 1)[-1] in GOVERNANCE_MODULES]
    return dict(path=str(path), sha256=digest(path), **counts(records),
        categories={key: counts(values) for key, values in sorted(groups.items())},
        governance_related=dict(**counts(governance), modules=sorted({item["classname"] for item in governance})),
        note="参数化本地测试数，不是独立漏洞数或模型正确率；跳过未验收")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--worker-source", type=Path, required=True)
    parser.add_argument("--junit", type=Path, action="append", default=[])
    parser.add_argument("--offline", type=Path)
    parser.add_argument("--evidence-directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("不覆盖历史摘要，请使用新路径")
    report = json.loads(args.routing.read_text(encoding="utf-8"))
    dataset = json.loads(args.cases.read_text(encoding="utf-8"))
    if report["dataset_sha256"] != digest(args.cases):
        parser.error("原报告与当前数据集SHA256不一致")
    output = dict(schema_version=1, generated_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        provenance=dict(routing_file=str(args.routing), routing_sha256=digest(args.routing),
            dataset_sha256=digest(args.cases), source_commit=report["source_commit"],
            source_dirty=report["source_dirty"], source_sha256=report["source_sha256"],
            source_unchanged_since_report=source_digest(args.worker_source) == report["source_sha256"],
            models=sorted({row["model_provider"]+"/"+row["model_name"] for row in report["results"] if row.get("live_model")}),
            python=platform.python_version(), platform=platform.platform(),
            dependencies={name: version(name) for name in ("langgraph", "httpx", "pydantic", "pytest")},
            analysis_script_sha256=digest(__file__),
            evaluator_script_sha256=digest(Path(__file__).with_name("evaluate_main_routing.py"))),
        routing=summarize_routing(report, dataset), regression=[junit_summary(path) for path in args.junit])
    if args.evidence_directory:
        output["archived_evidence"] = archive_evidence(
            [args.routing, args.cases, *args.junit, *([args.offline] if args.offline else [])], args.evidence_directory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in output.items() if key != "regression"}, ensure_ascii=False, indent=2))
    print("摘要："+str(args.output))


if __name__ == "__main__":
    main()
