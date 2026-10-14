"""对比已保存的报告；不运行模型，不修改原报告。"""
import argparse
import json
from pathlib import Path


def compare(before, after):
    for key in ("dataset_sha256", "fixed_now", "mode"):
        if before[key] != after[key]:
            raise ValueError(f"对比条件不一致：{key}")
    if not before["complete"] or not after["complete"]:
        raise ValueError("不能把不完整评测作为完整对比")
    def keys(report):
        return {(r["case_id"], r["repetition"]) for r in report["results"]}
    if keys(before) != keys(after):
        raise ValueError("用例范围或重复次数不一致")
    models = lambda report: {(r["model_provider"], r["model_name"]) for r in report["results"] if r["live_model"]}
    if models(before) != models(after):
        raise ValueError("实际模型配置不一致")
    metrics = ("semantic_routing_accuracy", "candidate_recall", "mean_candidate_count",
               "clarification_rate", "prohibited_handoff_rate", "illegal_call_rate", "mean_ms", "p95_ms", "repair_rate", "model_calls")
    return {"before": before["source_commit"], "after": after["source_commit"],
            "mode": before["mode"], "metrics": {key: {
                "before": before["summary"][key], "after": after["summary"][key],
                "delta": (after["summary"][key]-before["summary"][key]
                          if before["summary"][key] is not None and after["summary"][key] is not None else None)
            } for key in metrics}, "usage_before": before["summary"]["usage"],
            "usage_after": after["summary"]["usage"],
            "warning": "路由沙箱，不是端到端安全或上线证明；Mock 模式无模型准确率"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.dumps(compare(json.loads(args.before.read_text(encoding="utf-8")),
                                json.loads(args.after.read_text(encoding="utf-8"))), ensure_ascii=False, indent=2)
    if args.output:
        if args.output.exists():
            parser.error("输出文件已存在")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result, encoding="utf-8")
    print(result)
