"""Prepare shadow rebuilds from trusted source bytes, never activate production.

Call inside a tenant transaction. Source loading, identity and operator validation
are application responsibilities; this is not an anonymous/user-upload endpoint.
"""
from hashlib import sha256
from uuid import uuid4
from aegis_agent_worker.indexing.generation import GenerationConfig,build_manifest,digest
from aegis_agent_worker.repository.shadow_generation_repository import ShadowGenerationRepository
from aegis_agent_worker.harness.exceptions import HarnessError


class ShadowGenerationService:
    def __init__(self,repository=None): self.repo=repository or ShadowGenerationRepository()

    async def prepare(self,session,user,document_id,version_id,request_id,raw,config=None):
        if not isinstance(request_id,str) or not 1<=len(request_id)<=128:
            raise ValueError('invalid rebuild request id')
        config=GenerationConfig.model_validate(config or {})
        config.policy()  # Reject invalid overlap even on an idempotent retry.
        await self.repo.lock_request(session,user,request_id)
        source=await self.repo.owned(session,user,document_id,version_id)
        if sha256(raw).hexdigest()!=source['source_hash']:
            raise HarnessError('SHADOW_SOURCE_CHANGED','源文件与登记hash不一致')
        request_hash=digest(dict(document_id=str(document_id),version_id=str(version_id),
            source_hash=source['source_hash'],config=config.model_dump()))
        existing=await self.repo.existing(session,user,request_id)
        if existing:
            if existing['request_hash']!=request_hash:
                raise HarnessError('SHADOW_REQUEST_CONFLICT','相同重建请求不能改变来源或解析配置')
            return dict(generation_id=existing['id'],manifest_hash=existing['manifest_hash'],reused=True)
        gid=uuid4()
        manifest=build_manifest(document_id,version_id,gid,raw,source['title'],config)
        await self.repo.save(session,user,document_id,version_id,request_id,request_hash,manifest)
        return dict(generation_id=gid,manifest_hash=manifest['manifest_hash'],chunks=len(manifest['chunks']),reused=False)

    async def validate(self,session,user,document_id,version_id,generation_id,manifest_hash,reference):
        if not isinstance(reference,str) or not reference.strip() or len(reference)>200:
            raise ValueError('validation record reference required')
        if not isinstance(manifest_hash,str) or len(manifest_hash)!=64 or any(c not in '0123456789abcdef' for c in manifest_hash):
            raise ValueError('invalid manifest hash')
        # A trusted operator attests only the structural snapshot, NOT retrieval
        # quality/vector completion or permission to publish a production release.
        await self.repo.validate(session,user,document_id,version_id,generation_id,manifest_hash,reference)

    async def select(self,session,user,document_id,version_id,target,expected_revision,*,rollback=False):
        if type(expected_revision) is not int or expected_revision<0: raise ValueError('invalid revision')
        return await self.repo.select(session,user,document_id,version_id,target,expected_revision,rollback=rollback)
