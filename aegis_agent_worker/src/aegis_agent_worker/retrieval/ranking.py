"""候选先各路去重排序，再按chunk_id稳定RRF；分数不是正确率。"""
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class SearchHit:
    chunk_id: UUID
    score: float


def ordered_hits(hits, limit):
    best = {}
    for hit in hits:
        if hit.chunk_id not in best or hit.score > best[hit.chunk_id].score:
            best[hit.chunk_id] = hit
    return sorted(best.values(), key=lambda hit: (-hit.score, str(hit.chunk_id)))[:limit]


def fuse(lexical, vector, k):
    ranks, scores = {}, {}
    for method, hits in (("lexical", lexical), ("vector", vector)):
        for rank, hit in enumerate(hits, 1):
            if method in ranks.setdefault(hit.chunk_id, {}):
                continue
            ranks[hit.chunk_id][method] = rank
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0) + 1 / (k + rank)
    return [(chunk_id, scores[chunk_id], ranks[chunk_id])
            for chunk_id in sorted(scores, key=lambda key: (-scores[key], str(key)))]
