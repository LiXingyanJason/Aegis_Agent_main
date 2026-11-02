"""Offline A/B candidate trace. No quality claims and no external calls.

Token-overlap ranking is a proxy. Parameters are illustrative frozen defaults,
not selected using the test set. Production integration belongs to phase 3.5.
"""
import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID, uuid5, NAMESPACE_URL
from evaluate_chunking_baseline import freeze, covered
from aegis_agent_worker.indexing.contracts import SourceScope
from aegis_agent_worker.indexing.structural import parse_structure, StructuralPolicy, PARSER_VERSION
from aegis_agent_worker.indexing.chunker import search_tokens
from aegis_agent_worker.retrieval.expansion import expand_evidence


def run():
    _,docs,queries,digest=freeze()
    indexes={}; sources={}; enhanced={}
    for doc in docs.values():
        scope=SourceScope(document_id=uuid5(NAMESPACE_URL,doc['id']),version_id=UUID(doc['version']),
            generation_id=uuid5(UUID(doc['version']),'structure-v1:700:80'),parser_version=PARSER_VERSION)
        index=parse_structure(doc['text'],scope,StructuralPolicy())
        indexes[doc['id']]=index
        sources[scope]=doc['text']
        enhanced.update({e.chunk_id:e.text for e in index.enhanced(doc['title'])})
    traces=[]
    for q in queries:
        terms=set(search_tokens(q['query']).split())
        pool=[(doc_id,c) for doc_id,index in indexes.items() if docs[doc_id]['split']==q['split'] for c in index.chunks]
        for titles,extension in ((False,False),(True,False),(False,True),(True,True)):
            scored=[(len(terms & set(search_tokens(enhanced[c.chunk_id] if titles else c.content).split())),did,c) for did,c in pool]
            scored=sorted((r for r in scored if r[0]),key=lambda r:(-r[0],r[1],r[2].source_range.start))
            hits=[r[2] for r in scored[:5]]
            selected=[]; incomplete=False
            if extension:
                rel={cid:rs for did,index in indexes.items() if docs[did]['split']==q['split'] for cid,rs in index.relations.items()}
                result=expand_evidence([c.chunk_id for c in hits],[c for _,c in pool],sources,relations=rel)
                incomplete=result.incomplete
                for ctx in result.contexts:
                    did=next(d for d,idx in indexes.items() if idx.chunks[0].scope==ctx.scope)
                    selected.extend(dict(document_id=did,start_offset=r.start,end_offset=r.end) for r in ctx.ranges)
            else:
                used=0
                for c in hits:
                    if used+len(c.content)>3500: incomplete=True;continue
                    did=next(did for did,cc in pool if cc.chunk_id==c.chunk_id)
                    selected.append(dict(document_id=did,start_offset=c.source_range.start,end_offset=c.source_range.end))
                    used+=len(c.content)
            traces.append(dict(query_id=q['id'],split=q['split'],kind=q['kind'],titles=titles,expansion=extension,
                ranking=[dict(chunk_id=str(c.chunk_id),overlap_terms=score) for score,_,c in scored],
                ranges=selected,incomplete=incomplete,evidence_covered=[covered(s,selected) for s in q['evidence']]))
    return dict(dataset_sha256=digest,mode='offline_proxy_no_sql_vector_model',external_calls=0,
        structural_policy=asdict(StructuralPolicy()),
        chunks={did:[c.model_dump(mode='json') for c in index.chunks] for did,index in indexes.items()},
        traces=traces,limitations=['not_real_hybrid','no_authorization_validation','no_answer_generation','no_parameter_selection'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=run()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as handle:
        json.dump(report,handle,ensure_ascii=False,indent=2)
    print(json.dumps(dict(chunks=sum(len(cs) for cs in report['chunks'].values()),traces=len(report['traces']),external_calls=0)))
