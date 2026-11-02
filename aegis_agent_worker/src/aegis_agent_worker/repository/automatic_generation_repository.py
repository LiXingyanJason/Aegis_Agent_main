"""Atomic automatic structure preparation under the existing index-job lease."""
import json
from types import SimpleNamespace
from uuid import UUID
from sqlalchemy import text
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.repository.shadow_generation_repository import ShadowGenerationRepository


class AutomaticGenerationRepository:
    def __init__(self,index_repository):self.index=index_repository

    async def prepare(self,message,token,job,manifest,namespace,collection):
        gid=UUID(manifest['scope']['generation_id'])
        p=dict(id=message['job_id'],tenant=message['tenant_id'],user=message['user_id'],token=token,
               generation=gid,namespace=namespace,collection=collection,parser='structure-v1',total=len(manifest['chunks']))
        async with self.index.session(message) as s:
            alive=(await s.execute(text('''SELECT j.id FROM knowledge_index_jobs j
              JOIN knowledge_documents d ON d.id=j.document_id AND d.tenant_id=j.tenant_id
              JOIN knowledge_document_versions v ON v.id=j.version_id AND v.document_id=d.id AND v.tenant_id=d.tenant_id
              WHERE j.id=:id AND j.tenant_id=:tenant AND j.user_id=:user AND j.lease_token=:token
              AND j.status='running' AND j.lease_expires_at>now() AND d.user_id=:user AND d.deleted_at IS NULL
              AND NOT d.is_sensitive AND d.external_embedding_allowed AND v.content_hash=:hash
              AND (d.current_version_id IS NULL OR d.current_version_id=v.id)
              AND (j.generation_id IS NULL OR j.generation_id=:generation) FOR UPDATE OF j,d'''),dict(**p,hash=manifest['source_hash']))).first()
            if not alive:raise HarnessError('KNOWLEDGE_LEASE_LOST','自动代次任务租约或来源已失效')
            # The request is a code-generated job binding, not a user-selected path.
            repo=ShadowGenerationRepository()
            user=SimpleNamespace(id=message['user_id'],tenant_id=message['tenant_id'])
            existing=await repo.existing(s,user,'auto-index:'+str(message['job_id']))
            if existing:
                if str(existing['id'])!=str(gid) or existing['manifest_hash']!=manifest['manifest_hash']:
                    raise HarnessError('GENERATION_MANIFEST_CHANGED','重试不能更换自动代次结构或配置')
                rows=(await s.execute(text('SELECT payload FROM knowledge_generation_chunks WHERE generation_id=:generation AND tenant_id=:tenant ORDER BY position'),p)).mappings().all()
                payloads=[json.loads(r['payload']) if isinstance(r['payload'],str) else r['payload'] for r in rows]
                if payloads!=manifest['chunks']:raise HarnessError('GENERATION_MANIFEST_CHANGED','自动代次片段集合变化')
            else:
                await repo.save(s,user,job['document_id'],job['version_id'],'auto-index:'+str(message['job_id']),
                    manifest['manifest_hash'],manifest)
                await s.execute(text('''UPDATE knowledge_index_generations SET status='validated',validation_hash=manifest_hash,
                  validation_reference='automatic-structure-check-v1' WHERE id=:generation AND tenant_id=:tenant AND user_id=:user'''),p)
            bound=(await s.execute(text('''UPDATE knowledge_document_versions SET vector_namespace=:namespace,vector_collection=:collection
              WHERE id=:version AND tenant_id=:tenant AND (vector_namespace IS NULL OR vector_namespace=:namespace)
              AND (vector_collection IS NULL OR vector_collection=:collection) RETURNING id'''),dict(**p,version=job['version_id']))).first()
            if not bound:raise HarnessError('QDRANT_PROFILE_MISMATCH','自动代次集合绑定变化')
            await s.execute(text('''UPDATE knowledge_index_jobs SET generation_id=:generation,total_chunks=:total,parser_version=:parser,
              vector_namespace=:namespace,vector_collection=:collection WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND lease_token=:token'''),p)
            await s.execute(text('''INSERT INTO knowledge_generation_builds(generation_id,tenant_id,user_id,document_id,version_id)
              VALUES(:generation,:tenant,:user,:doc,:version) ON CONFLICT DO NOTHING'''),dict(**p,doc=job['document_id'],version=job['version_id']))
            # The unique parent has been claimed with a fresh lease. A running
            # child belongs to its previous crashed execution, so fence that
            # child's token instead of failing when its lease expires later.
            # Standalone generation scans exclude these parent-owned builds.
            await s.execute(text("UPDATE knowledge_generation_builds SET status='queued',lease_token=NULL,lease_expires_at=NULL WHERE generation_id=:generation AND tenant_id=:tenant AND user_id=:user AND status='running'"),p)
            # Retry reuses the same build and Embedding budget; counters persist.
            await s.execute(text("UPDATE knowledge_generation_builds SET status='queued',error_code=NULL WHERE generation_id=:generation AND tenant_id=:tenant AND user_id=:user AND status='failed' AND attempts<3"),p)
        return dict(generation_id=gid,tenant_id=message['tenant_id'],user_id=message['user_id'])

    async def progress(self,message,token,generation,require_ready=False):
        async with self.index.session(message) as s:
            row=(await s.execute(text('''UPDATE knowledge_index_jobs j SET processed_chunks=(SELECT count(*) FROM knowledge_generation_points p
              WHERE p.generation_id=:generation AND p.tenant_id=:tenant AND p.user_id=:user),updated_at=now()
              WHERE j.id=:id AND j.tenant_id=:tenant AND j.user_id=:user AND j.lease_token=:token AND j.status='running'
              AND j.lease_expires_at>now() AND j.generation_id=:generation
              AND (:ready=false OR EXISTS(SELECT 1 FROM knowledge_generation_builds b WHERE b.generation_id=:generation AND b.status='ready'))
              RETURNING j.id'''),dict(id=message['job_id'],tenant=message['tenant_id'],user=message['user_id'],token=token,generation=generation,ready=require_ready))).first()
            if not row:raise HarnessError('GENERATION_INCOMPLETE','自动代次尚未完成或父任务租约失效')
