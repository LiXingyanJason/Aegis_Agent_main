from uuid import UUID,uuid4
from aegis_agent_worker.embedding.contracts import EmbeddingProfile
from aegis_agent_worker.harness.embedding_executor import EmbeddingSubject
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.indexing.generation import digest
from aegis_agent_worker.harness.policies.embedding import INDEX_POLICY


def batches(chunks,max_items,max_chars):
    batch=[];used=0
    for chunk in chunks:
        chars=len(chunk['retrieval_text'])
        if chars>max_chars:raise HarnessError('EMBEDDING_INPUT_LIMIT','单个增强片段超过批次字符额度')
        if batch and (len(batch)>=max_items or used+chars>max_chars):
            yield batch;batch=[];used=0
        batch.append(chunk);used+=chars
    if batch:yield batch


class GenerationIndexService:
    def __init__(self,settings,repository,embedding,vector):
        self.settings,self.repo,self.embedding,self.vector=settings,repository,embedding,vector

    async def execute(self,item,heartbeat=None,progress=None):
        token=uuid4()
        if not await self.repo.claim(item,token):return {'status':'ignored'}
        try:
            if heartbeat is not None:await heartbeat()
            row,done=await self.repo.load(item,token)
            m=row['manifest'];body={k:v for k,v in m.items() if k!='manifest_hash'}
            if digest(body)!=m['manifest_hash']:raise HarnessError('GENERATION_MANIFEST_CHANGED','代次manifest不匹配')
            s=self.settings
            profile=EmbeddingProfile(row['embedding_provider'],row['embedding_model'],row['embedding_model_version'],row['embedding_dimensions'])
            if profile!=EmbeddingProfile(s.embedding_provider,s.embedding_model,s.embedding_model_version,s.embedding_dimensions):
                raise HarnessError('EMBEDDING_PROFILE_MISMATCH','代次模型空间不匹配')
            if row['vector_namespace']!=self.vector.namespace or row['vector_collection']!=s.qdrant_collection:
                raise HarnessError('QDRANT_PROFILE_MISMATCH','代次与原文模型集合绑定不一致')
            await self.vector.ensure_collection(profile)
            pending=[c for c in m['chunks'] if UUID(c['chunk_id']) not in done]
            batch_size=min(10,self.embedding.max_batch_items)
            for batch in batches(pending,batch_size,INDEX_POLICY.max_batch_chars):
                if heartbeat is not None:await heartbeat()
                await self.repo.load(item,token)  # Fresh authorization every batch.
                await self.repo.mark(item,token,[],self.vector.namespace,s.qdrant_collection)
                vectors=await self.embedding.embed(EmbeddingSubject('generation',item['generation_id'],item['tenant_id'],item['user_id']),
                    [c['retrieval_text'] for c in batch])
                if len(vectors)!=len(batch):raise HarnessError('EMBEDDING_RESPONSE_INVALID','向量数量不一致')
                await self.repo.load(item,token)
                await self.vector.upsert([dict(id=c['chunk_id'],vector=v,payload=dict(tenant_id=str(item['tenant_id']),
                    document_id=m['scope']['document_id'],version_id=m['scope']['version_id'],generation_id=str(item['generation_id']),content_hash=c['raw']['content_hash'],
                    model_provider=profile.provider,model=profile.model,model_version=profile.version,dimensions=profile.dimensions)) for c,v in zip(batch,vectors)])
                await self.repo.mark(item,token,[UUID(c['chunk_id']) for c in batch],self.vector.namespace,s.qdrant_collection)
                if progress is not None:await progress()
            await self.repo.mark(item,token,[],self.vector.namespace,s.qdrant_collection,complete=True)
            return {'status':'ready'}
        except Exception as e:
            code=getattr(e,'code','GENERATION_INDEX_FAILED')
            await self.repo.fail(item,token,code)
            return {'status':'failed','error_code':code}
