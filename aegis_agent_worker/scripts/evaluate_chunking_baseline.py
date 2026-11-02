"""Offline Phase 2 fixture freeze and paragraph-v1 baseline. No network or database.

Offsets are Python Unicode character offsets [start,end), not UTF-8 byte offsets.
Token-overlap ranking is a diagnostic proxy, NOT PostgreSQL FTS/Hybrid retrieval.
"""
import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

from aegis_agent_worker.indexing.chunker import chunk_text, search_tokens, PARSER_VERSION

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'tests/evaluation/data/chunking_phase2_v1.json'


def digest(value):
    return sha256(value).hexdigest()


def freeze(path=DATA):
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    docs = {}
    for d in data['documents']:
        if d['id'] in docs:
            raise ValueError('duplicate document')
        text = '\n\n'.join(s if isinstance(s, str) else s['text'] * s['repeat'] for s in d['segments'])
        docs[d['id']] = dict(id=d['id'], split=d['split'], title=d['title'], text=text,
            version=str(uuid5(NAMESPACE_URL, data['version'] + ':' + d['id'] + ':' + digest(text.encode()))),
            content_hash=digest(text.encode()))
    ids = set()
    queries = []
    for q in data['queries']:
        if q['id'] in ids:
            raise ValueError('duplicate query')
        ids.add(q['id'])
        spans = []
        for doc_id, role, quote in q['evidence']:
            doc = docs[doc_id]
            if doc['split'] != q['split'] or doc['text'].count(quote) != 1:
                raise ValueError('split leakage or ambiguous/missing evidence')
            start = doc['text'].index(quote)
            spans.append(dict(document_id=doc_id, version=doc['version'], role=role,
                start=start, end=start+len(quote), quote=quote))
        queries.append(dict(id=q['id'], split=q['split'], query=q['query'], query_rewrites=[],
            kind='answerable' if spans else 'no_answer', evidence=spans))
    return data, docs, queries, digest(raw)


def covered(span, chunks):
    ranges = sorted((c['start_offset'], c['end_offset']) for c in chunks
                    if c['document_id'] == span['document_id'])
    cursor = span['start']
    for start, end in ranges:
        if start > cursor:
            break
        cursor = max(cursor, end)
        if cursor >= span['end']:
            return True
    return False


def run(path=DATA):
    data, docs, queries, hash_ = freeze(path)
    chunks = []
    started = perf_counter()
    for doc in docs.values():
        for chunk in chunk_text(doc['text'], uuid5(NAMESPACE_URL, doc['version'])):
            chunks.append(dict(**asdict(chunk), document_id=doc['id'], version=doc['version']))
    elapsed = (perf_counter()-started)*1000
    traces = []
    for q in queries:
        terms = set(search_tokens(q['query']).split())
        pool = [c for c in chunks if docs[c['document_id']]['split'] == q['split']]
        ranked = sorted(pool, key=lambda c: (-len(terms & set(c['search_tokens'].split())),
                        c['document_id'], c['position']))
        ranked = [c for c in ranked if terms & set(c['search_tokens'].split())]
        selected, used = [], 0
        for c in ranked:
            if len(selected) == 5:
                break
            if used + len(c['content']) <= 3500:
                selected.append(c)
                used += len(c['content'])
        traces.append(dict(**q, ranking=[dict(chunk_id=str(c['id']), document_id=c['document_id'],
            position=c['position'], overlap_terms=len(terms & set(c['search_tokens'].split()))) for c in ranked],
            selected_ids=[str(c['id']) for c in selected], delivered_chars=used,
            evidence_covered=[covered(s, selected) for s in q['evidence']],
            single_chunk_contains=[any(c['document_id']==s['document_id'] and c['start_offset']<=s['start']
                and c['end_offset']>=s['end'] for c in chunks) for s in q['evidence']],
            answer=None, answer_status='not_executed', provider_usage=None))
    summary = {}
    for split in ('dev', 'test'):
        rows = [r for r in traces if r['split']==split and r['kind']=='answerable']
        flags = [f for r in rows for f in r['evidence_covered']]
        summary[split] = dict(answerable=len(rows), evidence_spans=len(flags),
            proxy_span_coverage=sum(flags)/len(flags),
            proxy_all_evidence_queries=sum(all(r['evidence_covered']) for r in rows)/len(rows),
            no_answer=sum(r['split']==split and r['kind']=='no_answer' for r in traces))
    return dict(dataset_version=data['version'], dataset_sha256=hash_, provenance=data['provenance'],
        configuration=dict(parser=PARSER_VERSION, size=700, overlap=80, maximum=512,
            top_k=5, context_chars=3500, query_rewrites='disabled', random_seed=None,
            ranking='offline_token_overlap_proxy_not_fts_or_hybrid', embedding=None, answer_model=None,
            external_calls=0, production_rrf_k_reference=60, production_candidates_reference=20),
        code_sha256=digest(Path(__file__).read_bytes()),
        chunker_sha256=digest((ROOT/'src/aegis_agent_worker/indexing/chunker.py').read_bytes()),
        documents=list(docs.values()), chunks=chunks, queries=traces, summary=summary,
        measurements=dict(chunking_ms=elapsed, document_chars=sum(len(d['text']) for d in docs.values()),
            chunk_chars=sum(len(c['content']) for c in chunks), chunks=len(chunks)),
        pending=['real_postgresql_qdrant_embedding_retrieval', 'answer_generation_and_review',
                 'independent_label_review', 'provider_model_and_budget_freeze_before_external_run'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Refuse accidental replacement of a frozen run.
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(dict(summary=report['summary'], measurements=report['measurements'],
                         dataset_sha256=report['dataset_sha256']), ensure_ascii=False))
