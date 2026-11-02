"""Bounded pure selector over a server-authorized structural snapshot.

Not an authorization layer. SQL-backed snapshot loading is required before live use.
No parent expansion, recursive traversal or cross-version extension.
"""
from dataclasses import dataclass
from uuid import UUID
from aegis_agent_worker.indexing.contracts import EvidenceContext, EvidenceOrigin, merge_ranges


@dataclass(frozen=True)
class ExpansionPolicy:
    max_direct: int = 5
    max_expansions: int = 6
    neighbor_distance: int = 1
    max_chars: int = 3500
    max_bytes: int = 12000

    def __post_init__(self):
        if not (1<=self.max_direct<=5 and 0<=self.max_expansions<=27 and
                0<=self.neighbor_distance<=3 and 1<=self.max_chars<=12800 and 1<=self.max_bytes<=48000):
            raise ValueError('invalid expansion limits')


@dataclass(frozen=True)
class ExpansionResult:
    contexts: tuple[EvidenceContext, ...]
    chars: int
    bytes: int
    incomplete: bool


def expand_evidence(direct_ids: list[UUID], authorized_chunks, source_by_scope, *, policy=None, relations=None):
    """Inputs must be a fresh, bounded authorized snapshot, never model-supplied.

    relations contain original header/lead ranges; they are referenced, not copied
    into a raw chunk. Caller must authorize the entire canonical source snapshot.
    """
    p = policy or ExpansionPolicy()
    if len(direct_ids)>p.max_direct or len(set(direct_ids))!=len(direct_ids):
        raise ValueError('invalid direct hits')
    chunks = {c.chunk_id:c for c in authorized_chunks}
    if len(chunks)!=len(authorized_chunks) or len(chunks)>512:
        raise ValueError('invalid authorized snapshot')
    for c in chunks.values():
        c.verify_source(source_by_scope[c.scope])
    groups, reasons = {}, {}
    missing = False
    def try_add(c, origin):
        key = c.scope
        old = groups.get(key,([],[]))
        ranges = merge_ranges([*old[0],c.source_range,*(relations or {}).get(c.chunk_id,())])
        origins = [*old[1],origin]
        if len(ranges)>16 or len(origins)>32:
            return False
        candidate = dict(groups); candidate[key]=(ranges,origins)
        count = size = 0
        for scope,(rs,_) in candidate.items():
            source = source_by_scope[scope]
            for r in rs:
                if r.end>len(source): raise ValueError('relation outside source')
                body = source[r.start:r.end]
                count += len(body); size += len(body.encode('utf-8'))
        if count>p.max_chars or size>p.max_bytes:
            return False
        groups[key]=(ranges,origins)
        return True
    accepted = []
    for rank,cid in enumerate(direct_ids,1):
        if cid not in chunks:
            missing = True
            continue
        c=chunks[cid]
        if try_add(c,EvidenceOrigin(kind='direct',chunk_id=cid,rrf_rank=rank)):
            accepted.append(c)
        else:
            reasons[c.scope]='budget'
    proposals = []
    for rank,trigger in enumerate(accepted):
        same_scope=sorted((c for c in chunks.values() if c.scope==trigger.scope),key=lambda c:c.source_range.start)
        position = next(i for i,c in enumerate(same_scope) if c.chunk_id==trigger.chunk_id)
        for index,c in enumerate(same_scope):
            if c.chunk_id in direct_ids: continue
            same_clause = bool(trigger.clause_id and c.clause_id==trigger.clause_id)
            adjacent = c.heading_path==trigger.heading_path and abs(index-position)<=p.neighbor_distance
            if same_clause or adjacent:
                proposals.append((0 if same_clause else 1,rank,abs(index-position),c.source_range.start,
                    c,trigger,'same_clause' if same_clause else 'adjacent'))
    seen=set(direct_ids); added=0
    for _,_,_,_,c,trigger,kind in sorted(proposals,key=lambda v:v[:4]):
        if c.chunk_id in seen: continue
        seen.add(c.chunk_id)
        if added>=p.max_expansions or not try_add(c,EvidenceOrigin(kind=kind,chunk_id=c.chunk_id,trigger_chunk_id=trigger.chunk_id)):
            reasons[c.scope]='budget'
        else:
            added+=1
    contexts=[]; chars=size=0
    for scope,(ranges,origins) in groups.items():
        reason=reasons.get(scope) or ('source_unavailable' if missing else None)
        context=EvidenceContext(scope=scope,ranges=tuple(ranges),origins=tuple(origins),
            completeness='incomplete' if reason else 'not_assessed',reason=reason).verify_origins(chunks)
        for part in context.source_parts(source_by_scope[scope]):
            chars+=len(part);size+=len(part.encode())
        contexts.append(context)
    return ExpansionResult(tuple(contexts),chars,size,missing or bool(reasons))
