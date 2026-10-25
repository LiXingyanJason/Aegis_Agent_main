import math
import random
from statistics import mean


def excludes_target(evidence, document_id, chunk_id=None):
    """片段级失效不应误要求整份文档消失；文档级撤权则排除全部片段。"""
    return all(item.chunk_id != chunk_id if chunk_id is not None else item.document_id != document_id for item in evidence)


def ranking_metrics(ranking, qrels, k=5):
    if not qrels:
        raise ValueError("无答案/权限负例不能混入有答案指标分母")
    if len(ranking) != len(set(ranking)):
        raise ValueError("排名必须先去重")
    grades = [qrels.get(key, 0) for key in ranking[:k]]
    relevant = {key for key, grade in qrels.items() if grade > 0}
    if not relevant:
        raise ValueError("必须至少存在一个正相关片段")
    ideal = sorted(qrels.values(), reverse=True)[:k]
    dcg = lambda values: sum((2**grade-1)/math.log2(index+2) for index, grade in enumerate(values))
    return dict(recall=sum(grade > 0 for grade in grades)/len(relevant),
        mrr=next((1/(index+1) for index, grade in enumerate(grades) if grade > 0), 0),
        hit=float(any(grades)), ndcg=dcg(grades)/dcg(ideal))


def summarize(rows, split):
    selected = [row for row in rows if row["kind"] == "answerable" and row["split"] == split]
    return {arm: {metric: mean(row["metrics"][arm][metric] for row in selected) if selected else None
                   for metric in ("recall", "mrr", "hit", "ndcg")}
            for arm in ("lexical", "vector", "rrf")}


def paired_intervals(rows, draws=10000, seed=20261022):
    """两个查询共享同一文档，按文档整组重采样并保持各方法配对。"""
    groups = {}
    for row in rows:
        if row["kind"] == "answerable" and row["split"] == "test":
            groups.setdefault(row["document_id"], []).append(row)
    clusters = list(groups.values())
    if not clusters:
        return {}
    rng = random.Random(seed)
    samples = [[row for group in rng.choices(clusters, k=len(clusters)) for row in group] for _ in range(draws)]
    result = {}
    for baseline in ("lexical", "vector"):
        result[baseline] = {}
        for metric in ("recall", "mrr", "hit", "ndcg"):
            values = sorted(mean(row["metrics"]["rrf"][metric]-row["metrics"][baseline][metric] for row in sample)
                            for sample in samples)
            all_rows = [row for group in clusters for row in group]
            delta = mean(row["metrics"]["rrf"][metric]-row["metrics"][baseline][metric] for row in all_rows)
            base = mean(row["metrics"][baseline][metric] for row in all_rows)
            result[baseline][metric] = dict(delta=delta, delta_percentage_points=100*delta,
                relative_change=delta/base if base else None,
                ci95=[values[max(0, math.ceil(draws*.025)-1)], values[math.ceil(draws*.975)-1]])
    return dict(method="paired_document_cluster_bootstrap", draws=draws, seed=seed, groups=len(clusters), comparisons=result)
