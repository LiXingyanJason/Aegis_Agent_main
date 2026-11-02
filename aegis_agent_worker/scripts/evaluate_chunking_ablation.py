"""Phase 2 offline factorial ablation, not SQL/vector/RRF or answer evaluation."""
import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import random
from statistics import mean
from time import perf_counter
from uuid import UUID, NAMESPACE_URL, uuid5

from evaluate_chunking_baseline import freeze, covered
from aegis_agent_worker.indexing.chunker import chunk_text, search_tokens
from aegis_agent_worker.indexing.contracts import RawChunk, SourceScope, SourceRange
from aegis_agent_worker.indexing.structural import parse_structure, StructuralPolicy
from aegis_agent_worker.retrieval.expansion import expand_evidence

ARMS = [(s,t,e) for s in (False,True) for t in (False,True) for e in (False,True)]


def arm_name(s,t,e):
    return f"{'structure' if s else 'paragraph'}-title{int(t)}-expand{int(e)}"


def normalize(rows):
    result=[]
    for did in sorted({r['document_id'] for r in rows}):
        for r in sorted((r for r in rows if r['document_id']==did),key=lambda r:r['start_offset']):
            if result and result[-1]['document_id']==did and r['start_offset']<=result[-1]['end_offset']:
                result[-1]['end_offset']=max(r['end_offset'],result[-1]['end_offset'])
            else: result.append(dict(r))
    return result


def metrics(q, ranges):
    flags=[covered(s,ranges) for s in q['evidence']]
    limited=[f for s,f in zip(q['evidence'],flags) if s['role'] in ('condition','exception','cross_reference')]
    return dict(span_coverage=mean(flags) if flags else None,
        all_required=int(all(flags)) if flags else None,
        qualifiers=mean(limited) if limited else None)


def paired_interval(rows, arm, baseline, field, draws=2000):
    # Query-paired interval; multi-document questions preclude independent document groups.
    pairs={}
    for r in rows:
        if r['split']=='test' and r['kind']=='answerable' and r['metrics'][field] is not None:
            pairs.setdefault(r['query_id'],{})[r['arm']]=r['metrics'][field]
    diffs=[p[arm]-p[baseline] for p in pairs.values() if arm in p and baseline in p]
    if not diffs: return None
    rng=random.Random(20261102)
    samples=sorted(mean(rng.choices(diffs,k=len(diffs))) for _ in range(draws))
    return dict(n=len(diffs),delta=mean(diffs),ci95=[samples[int(.025*draws)],samples[int(.975*draws)]],
                method='query_paired_bootstrap_exploratory_correlated_topics_not_adjusted')


def run():
    _,docs,queries,digest=freeze()
    indexes={}; sources={}; costs={}
    for structure in (False,True):
        started=perf_counter(); chunks=[]; relations={}
        for did,d in docs.items():
            parser='structure-v1' if structure else 'paragraph-v1'
            scope=SourceScope(document_id=uuid5(NAMESPACE_URL,did),version_id=UUID(d['version']),
                generation_id=uuid5(UUID(d['version']),parser+':700:80'),parser_version=parser)
            sources[scope]=d['text']
            if structure:
                index=parse_structure(d['text'],scope,StructuralPolicy())
                cs=index.chunks;relations.update(index.relations)
            else:
                cs=[RawChunk(scope=scope,structure='paragraph',heading_path=(c.section,) if c.section else (),
                    source_range=SourceRange(start=c.start_offset,end=c.end_offset),content=c.content,content_hash=c.content_hash)
                    for c in chunk_text(d['text'],UUID(d['version']))]
            chunks.extend((did,c) for c in cs)
        indexes[structure]=(chunks,relations)
        costs[str(structure)]=dict(chunks=len(chunks),chunk_chars=sum(len(c.content) for _,c in chunks),
            build_ms=(perf_counter()-started)*1000,
            enhancement_chars=sum(len('\n'.join((docs[did]['title'],*c.heading_path,c.content))) for did,c in chunks))
    traces=[]
    for q in queries:
        terms=set(search_tokens(q['query']).split())
        for structure,titles,extension in ARMS:
            started=perf_counter();chunks,relations=indexes[structure]
            pool=[(did,c) for did,c in chunks if docs[did]['split']==q['split']]
            ranked=[]
            for did,c in pool:
                text='\n'.join((docs[did]['title'],*c.heading_path,c.content)) if titles else c.content
                score=len(terms & set(search_tokens(text).split()))
                if score: ranked.append((score,did,c))
            ranked.sort(key=lambda r:(-r[0],r[1],r[2].source_range.start))
            selected=[];incomplete=False
            if extension:
                result=expand_evidence([c.chunk_id for _,_,c in ranked[:5]],[c for _,c in pool],sources,relations=relations)
                incomplete=result.incomplete
                for ctx in result.contexts:
                    did=next(did for did,c in pool if c.scope==ctx.scope)
                    selected.extend(dict(document_id=did,start_offset=r.start,end_offset=r.end) for r in ctx.ranges)
            else:
                selected=[dict(document_id=did,start_offset=c.source_range.start,end_offset=c.source_range.end)
                          for _,did,c in ranked[:5]]
            # Identical final protocol: at most 5 normalized ranges, 3500 chars,
            # 12000 serialized UTF-8 bytes including source references and text.
            normalized=normalize(selected);delivered=[];used=0
            for r in normalized:
                part=docs[r['document_id']]['text'][r['start_offset']:r['end_offset']]
                candidate=[*delivered,dict(**r,text=part)]
                size=len(json.dumps(candidate,ensure_ascii=False).encode())
                if len(candidate)>5 or used+len(part)>3500 or size>12000:
                    incomplete=True;continue
                delivered=candidate;used+=len(part)
            ranges=[{k:v for k,v in r.items() if k!='text'} for r in delivered]
            relevant=lambda c,did:any(s['document_id']==did and c.source_range.start<s['end'] and c.source_range.end>s['start'] for s in q['evidence'])
            first=next((i for i,(_,did,c) in enumerate(ranked[:5],1) if relevant(c,did)),None)
            m=metrics(q,ranges)
            m['mrr5_overlap']=1/first if first else 0 if q['evidence'] else None
            traces.append(dict(query_id=q['id'],split=q['split'],kind=q['kind'],arm=arm_name(structure,titles,extension),
                ranking=[dict(chunk_id=str(c.chunk_id),document_id=did,score=score) for score,did,c in ranked],
                ranges=ranges,metrics=m,incomplete=incomplete,delivered_chars=used,
                result_bytes=len(json.dumps(delivered,ensure_ascii=False).encode()),
                duplicate_chars_removed=sum(r['end_offset']-r['start_offset'] for r in selected)-sum(r['end_offset']-r['start_offset'] for r in normalized),
                retrieval_ms=(perf_counter()-started)*1000,answer=None,provider_usage=None))
    summaries={}
    for split in ('dev','test'):
        summaries[split]={}
        for flags in ARMS:
            name=arm_name(*flags);rs=[r for r in traces if r['arm']==name and r['split']==split]
            vals=[r for r in rs if r['kind']=='answerable']
            durations=sorted(r['retrieval_ms'] for r in rs)
            summaries[split][name]={field:mean(r['metrics'][field] for r in vals if r['metrics'][field] is not None)
                for field in ('span_coverage','all_required','qualifiers','mrr5_overlap')}
            summaries[split][name].update(n=len(vals),mean_chars=mean(r['delivered_chars'] for r in rs),
                incomplete=sum(r['incomplete'] for r in rs),mean_ms=mean(durations),p95_ms=durations[min(len(durations)-1,int(.95*len(durations)))],
                no_answer_candidates=sum(bool(r['ranking']) for r in rs if r['kind']=='no_answer'))
    return dict(dataset_sha256=digest,script_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        mode='offline_lexical_overlap_proxy',external_calls=0,seed=20261102,parameters=dict(size=700,overlap=80,top_k=5,context_chars=3500,result_bytes=12000),
        costs=costs,summary=summaries,traces=traces,
        paired={arm_name(*f):paired_interval(traces,arm_name(*f),arm_name(False,False,False),'span_coverage') for f in ARMS},
        failures=[dict(query_id=r['query_id'],arm=r['arm'],metrics=r['metrics'],incomplete=r['incomplete']) for r in traces
                  if r['kind']=='answerable' and (r['metrics']['all_required']!=1 or r['incomplete'])],
        pending=['real_sql_vector_rrf','answer_quality_and_citations','sql_acl_revocation_retry_regressions','quality_cost_release_thresholds'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); report=run()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as handle:
        json.dump(report,handle,ensure_ascii=False,indent=2)
    print(json.dumps(dict(summary=report['summary']['test'],costs=report['costs'],failures=len(report['failures'])),ensure_ascii=False))
