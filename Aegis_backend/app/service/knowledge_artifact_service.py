"""来源读取时重验，失效快照不能作为新的原文读取兜底。"""
from app.repository.knowledge_source_repository import KnowledgeSourceRepository
from app.vo.knowledge_source_vo import KnowledgeSourceSnapshotPayload,KnowledgeSourceSnapshot


class KnowledgeArtifactService:
    def __init__(self,repository=None):
        self._repository=repository or KnowledgeSourceRepository()

    async def refresh(self,session,user,artifact):
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
