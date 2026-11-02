from uuid import uuid5
from aegis_agent_worker.indexing.generation import GenerationConfig,build_manifest
from aegis_agent_worker.repository.automatic_generation_repository import AutomaticGenerationRepository
from aegis_agent_worker.harness.exceptions import HarnessError


class AutomaticGenerationIndexService:
    def __init__(self,settings,index_repository,generation_index,vector,profile):
        self.settings,self.index,self.builder,self.vector,self.profile=settings,index_repository,generation_index,vector,profile
        self.repo=AutomaticGenerationRepository(index_repository)

    async def execute(self,message,token,job,raw):
        pinned=job['index_config']
        if not isinstance(pinned,dict) or set(pinned)!={'strategy','source_title','generation'} or pinned['strategy']!='structure-v1':
            raise HarnessError('KNOWLEDGE_INDEX_CONFIG_INVALID','自动索引配置无效')
        config=GenerationConfig.model_validate(pinned['generation'])
        gid=uuid5(job['id'],'automatic-generation-v1')
        manifest=build_manifest(job['document_id'],job['version_id'],gid,raw,pinned['source_title'],config)
        current=await self.vector.ensure_collection(self.profile)
        await self.index.bind_space(self.vector.namespace,self.settings.qdrant_collection,self.profile,current.get('points_count'))
        item=await self.repo.prepare(message,token,job,manifest,self.vector.namespace,self.settings.qdrant_collection)
        async def heartbeat():await self.index.renew(message,token,self.settings.knowledge_index_lease_seconds)
        async def progress():await self.repo.progress(message,token,gid)
        result=await self.builder.execute(item,heartbeat=heartbeat,progress=progress)
        if result['status']=='failed':raise HarnessError(result['error_code'],'自动代次构建失败')
        await self.repo.progress(message,token,gid,require_ready=True)
        await self.index.complete(message,token,job)
        return dict(status='succeeded',total_chunks=len(manifest['chunks']),generation_id=str(gid),published=False)
