"""Direct-first augmentation. Extensions cannot displace or rewrite a direct hit."""
import json
from uuid import UUID
from aegis_agent_worker.agent.contracts.knowledge import EvidencePart
from aegis_agent_worker.indexing.contracts import RawChunk
from aegis_agent_worker.harness.context import get_execution


class GenerationContextService:
    def __init__(self,repository):self.repo=repository

    async def augment(self,context,evidence,metadata):
        policy=get_execution().policy_bundle.generations
        if not policy.enabled:return evidence
        result=list(evidence);seen={e.chunk_id for e in evidence};snapshots={}
        for index,e in enumerate(evidence):
            if e.generation_id is None:continue
            if e.generation_id not in snapshots:
                snapshots[e.generation_id]=await self.repo.structure_snapshot(context,e.generation_id)
            rows=snapshots[e.generation_id]
            parsed=[(r,RawChunk.model_validate(r['raw'])) for r in rows]
            position=next((i for i,(_,c) in enumerate(parsed) if c.chunk_id==e.chunk_id),None)
            if position is None:continue
            row,trigger=parsed[position]
            proposals=[]
            for i,(r,c) in enumerate(parsed):
                if c.chunk_id in seen:continue
                same=bool(trigger.clause_id and trigger.clause_id==c.clause_id)
                adjacent=c.heading_path==trigger.heading_path and abs(i-position)==1
                related=any(c.source_range.start<=ref['start'] and c.source_range.end>=ref['end'] for ref in row['relations'])
                if same or adjacent or related:
                    proposals.append((0 if same else 1 if related else 2,abs(i-position),c.source_range.start,c,
                                      'same_clause' if same else 'related' if related else 'adjacent'))
            parts=[];incomplete=False
            for _,_,_,c,kind in sorted(proposals,key=lambda r:r[:3]):
                if c.chunk_id in seen:continue
                part=EvidencePart(chunk_id=c.chunk_id,start_offset=c.source_range.start,end_offset=c.source_range.end,
                    excerpt=c.content,content_hash=c.content_hash,origin=kind,trigger_chunk_id=e.chunk_id)
                candidate=e.model_copy(update={'parts':[*parts,part],'context_incomplete':False})
                trial=[*result];trial[index]=candidate
                chars=sum(len(x.excerpt)+sum(len(p.excerpt) for p in x.parts) for x in trial)
                size=len(json.dumps(dict(evidence=[x.model_dump(mode='json') for x in trial],retrieval=metadata),ensure_ascii=False,default=str).encode())
                if len(parts)>=policy.max_parts_per_source or chars>policy.max_context_chars or size>policy.max_result_bytes:
                    incomplete=True;continue
                parts.append(part);seen.add(c.chunk_id)
            # model_copy doesn't add unset nested fields to the serializer unless
            # explicitly marked; model_copy(update=...) does set these fields.
            updated=e.model_copy(update={'parts':parts,'context_incomplete':incomplete})
            trial=[*result];trial[index]=updated
            if len(json.dumps(dict(evidence=[x.model_dump(mode='json') for x in trial],retrieval=metadata),ensure_ascii=False,default=str).encode())<=policy.max_result_bytes:
                result=trial
        return result
