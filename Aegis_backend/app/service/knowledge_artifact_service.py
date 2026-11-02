"""来源读取时重验，失效快照不能作为新的原文读取兜底。"""
from app.repository.knowledge_source_repository import KnowledgeSourceRepository
from app.vo.knowledge_source_vo import KnowledgeSourceSnapshotPayload,KnowledgeSourceSnapshot


class KnowledgeArtifactService:
    def __init__(self,repository=None):
        self._repository=repository or KnowledgeSourceRepository()

    async def refresh(self,session,user,artifact):
        if artifact.get('artifact_type')=='knowledge_sources_v2':
            return await self._refresh_v2(session,user,artifact)
        try:
            snapshot=KnowledgeSourceSnapshotPayload.model_validate(artifact['payload'])
        except (ValueError,TypeError):
            return {**artifact,'payload':{'sources':[],'availability':'unavailable','has_conflict':False}}
        prior={item.chunk_id:item for item in snapshot.sources}
        rows=await self._repository.load_visible(session,user,list(prior))
        accepted={}
        for row in rows:
            if row['chunk_id'] not in prior:
                continue
            try:
                current=KnowledgeSourceSnapshot(source_no=prior[row['chunk_id']].source_no,**dict(row))
            except ValueError:
                continue
            if current==prior[current.chunk_id]:
                accepted[current.chunk_id]=current
        # 历史回答[n]的编号不能重排；任一标题/版本/hash/位置/摘录改变均不替换旧引用。
        sources=[accepted[item.chunk_id].model_dump(mode='json') for item in snapshot.sources if item.chunk_id in accepted]
        availability='available' if len(sources)==len(snapshot.sources) else ('partial' if sources else 'unavailable')
        return {**artifact,'payload':{'sources':sources,'availability':availability,'has_conflict':snapshot.has_conflict}}

    async def _refresh_v2(self,session,user,artifact):
        from app.vo.knowledge_source_vo import KnowledgeSourcesV2Payload
        try:snapshot=KnowledgeSourcesV2Payload.model_validate(artifact['payload'])
        except (ValueError,TypeError):return {**artifact,'payload':dict(schema_version=2,sources=[],availability='unavailable',has_conflict=False)}
        ids={s.chunk_id for s in snapshot.sources}|{p.chunk_id for s in snapshot.sources for p in s.parts}
        rows=await self._repository.load_visible_v2(session,user,list(ids))
        found={r['chunk_id']:dict(r) for r in rows};accepted=[]
        for source in snapshot.sources:
            primary=found.get(source.chunk_id)
            fields=('document_id','version_id','generation_id','title','version_label','section','start_offset','end_offset','excerpt','content_hash')
            if primary is None or any(primary[k]!=getattr(source,k) for k in fields):continue
            good=True
            for p in source.parts:
                row=found.get(p.chunk_id)
                if row is None or (row['document_id'],row['version_id'],row['generation_id'])!=(source.document_id,source.version_id,source.generation_id):good=False;break
                if p.trigger_chunk_id!=source.chunk_id or any(row[k]!=getattr(p,k) for k in ('start_offset','end_offset','excerpt','content_hash')):good=False;break
            if good:accepted.append(source.model_dump(mode='json'))
        return {**artifact,'payload':dict(schema_version=2,sources=accepted,has_conflict=snapshot.has_conflict,
            availability='available' if len(accepted)==len(snapshot.sources) else 'partial' if accepted else 'unavailable')}
