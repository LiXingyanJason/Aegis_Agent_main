"""E04离线证据归档；验证原始排名未被断言修正改变，不发任何网络请求。"""
import argparse
from datetime import datetime
from pathlib import Path
import json
from statistics import mean
from zoneinfo import ZoneInfo

from summarize_resume_experiments import archive_evidence, digest, source_digest, percentile, junit_summary


def check_reports(original, verified):
    for key in ("dataset_sha256", "experiment_id", "results", "test", "dev", "paired_bootstrap", "no_answer", "policy", "source_sha256"):
        if original[key] != verified[key]: raise ValueError("原始排名/标注/参数/版本被改动："+key)
    if not verified["complete"] or not verified["cleanup"] or not verified["verification"]["passed"]:
        raise ValueError("实验或清理未完成")


def summarize(verified):
    rows = verified["results"]
    safety = [row for row in rows if row["kind"] == "acl"]
    latency = [row["hybrid_ms"] for row in rows]
    variant = {}
    for name in ("direct", "semantic"):
        selected = [row for row in rows if row["kind"] == "answerable" and row["split"] == "test" and row["query_id"].endswith(name)]
        variant[name] = {arm: {metric: mean(row["metrics"][arm][metric] for row in selected)
                             for metric in ("recall", "mrr", "hit", "ndcg")} for arm in ("lexical", "vector", "rrf")}
    return dict(dev=verified["dev"], test=verified["test"], test_query_variants=variant,
        paired_bootstrap=verified["paired_bootstrap"], no_answer=verified["no_answer"],
        safety=dict(acl_queries=len(safety), leaked_canary_queries=sum(bool(row["leaked_canaries"]) for row in safety),
            forbidden_scope_queries=sum(row["forbidden_versions_in_scope"] > 0 for row in rows),
            direct_source_checks=verified["direct_source_checks"], governance_checks=verified["governance_checks"],
            original_governance_checks=verified["original_governance_checks"], injected_vector_faults=verified["injected_vector_faults"],
            rls_tested=verified["rls_tested"]),
        hybrid_latency_ms=dict(observations=len(rows), mean=mean(latency), p50=percentile(latency,.5), p95=percentile(latency,.95),
            note="单次真实Hybrid管线；消融候选重放不能做完整单路时延对比"),
        embedding=dict(requests=verified["requests"]["embedding_requests"],
            reported_total_tokens=(sum(int(item["total_tokens"]) for item in verified["audit"] if item["total_tokens"] is not None)
                if any(item["total_tokens"] is not None for item in verified["audit"]) else None),
            unknown_usage=sum(item["unknown_usage"] for item in verified["audit"]), audit=verified["audit"]),
        limitations=["构造式合成相关性，未独立盲审", "测试集36条/18份文档，非生产分布", "纯向量本集优于RRF",
                     "6条无答案均返回候选，未测生成回答幻觉/拒答", "不验证数据库角色RLS/HTTP来源详情接口", "无Rerank/LLM改写/上传切块评测"])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--verified", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--worker-source", type=Path, required=True)
    parser.add_argument("--junit", type=Path, action="append", default=[])
    parser.add_argument("--evidence-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args=parser.parse_args()
    if args.output.exists(): parser.error("不覆盖历史实验摘要")
    original=json.loads(args.original.read_text(encoding="utf-8"))
    verified=json.loads(args.verified.read_text(encoding="utf-8"))
    check_reports(original, verified)
    if digest(args.dataset) != original["dataset_sha256"] or digest(args.original) != verified["verification"]["original_sha256"]:
        parser.error("冻结数据或原报告hash不匹配")
    output=dict(version="e04-summary-v1", generated_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        source_sha256=verified["source_sha256"], source_unchanged_since_report=source_digest(args.worker_source)==verified["source_sha256"],
        profile=verified["profile"], schema_contract=verified["schema_contract"], complete=True, cleanup=verified["cleanup"],
        summary=summarize(verified), regression=[junit_summary(path) for path in args.junit],
        provenance=dict(original_sha256=digest(args.original), verified_sha256=digest(args.verified), dataset_sha256=digest(args.dataset)),
        archived_evidence=archive_evidence([args.original,args.verified,args.dataset,*args.junit],args.evidence_directory))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(dict(test=verified["test"], requests=verified["requests"], cleanup=verified["cleanup"], output=str(args.output)),ensure_ascii=False,indent=2))


if __name__ == "__main__": main()
