"""纯统计：未知用量不当零，Mock 结果不冒充模型准确率。"""
import math
from statistics import mean


def summarize(rows, mode):
    def ratio(values):
        return sum(values) / len(values) if values else None
    model_rows = [r for r in rows if r["entry"] == "conversation" and r["scenario"] == "semantic"]
    latency = sorted(r["duration_ms"] for r in model_rows)
    calls = [call for r in rows for call in r["calls"]]
    recall = [r["candidate_recall"] for r in model_rows if r["candidate_recall"] is not None]
    return {
        "runs": len(rows), "passed": sum(r["passed"] for r in rows),
        "deterministic_pass_rate": ratio([r["passed"] for r in rows]),
        "semantic_routing_accuracy": ratio([r["passed"] for r in model_rows]) if mode == "live" else None,
        "candidate_recall": ratio(recall),
        "mean_candidate_count": mean(r["candidate_count"] for r in model_rows) if model_rows else None,
        "clarification_rate": ratio([r["decision"] == "clarify" for r in model_rows]),
        "unnecessary_clarifications": sum(r["decision"] == "clarify" and not r["passed"] for r in model_rows),
        "prohibited_handoff_runs": sum(bool(r["illegal_calls"]) for r in rows),
        "prohibited_handoff_rate": ratio([bool(r["illegal_calls"]) for r in rows]),
        # 沙箱没有真实业务调用；不能把固定为零的替身计数当生产非法调用率。
        "illegal_call_rate": None,
        "mean_ms": mean(latency) if latency else None,
        "p95_ms": latency[max(0, math.ceil(len(latency) * .95) - 1)] if latency else None,
        "repair_rate": ratio([r["repair_count"] > 0 for r in model_rows]),
        "model_calls": len(calls),
        "usage": {name: {
            "reported_calls": sum(c[name] is not None for c in calls),
            "unknown_calls": sum(c[name] is None for c in calls),
            "reported_sum": sum(c[name] for c in calls if c[name] is not None)
                            if any(c[name] is not None for c in calls) else None,
        } for name in ("prompt_tokens", "completion_tokens", "total_tokens")},
        "scope": "route_only_with_in_memory_facts_not_end_to_end",
        "ready_for_release": False,
    }
