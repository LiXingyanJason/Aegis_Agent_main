from contextlib import asynccontextmanager
from uuid import uuid4
from sqlalchemy import text
from aegis_agent_worker.config.database import TenantContext,tenant_transaction
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.indexing.generation_sql import GENERATION_SOURCE_STATE


class GenerationBuildRepository:
    def __init__(self,database):self.database=database
    @asynccontextmanager
    async def session(self,item):
        async with self.database.session() as s:
            async with tenant_transaction(s,TenantContext(item['tenant_id'],item['user_id'])):yield s

    async def queued(self):
        # Same trusted scheduler boundary as the existing index worker; metadata only.
        async with self.database.session() as s:
            return [dict(r) for r in (await s.execute(text('''SELECT generation_id,tenant_id,user_id
              FROM knowledge_generation_builds b WHERE (status='queued' OR
              (status='running' AND lease_expires_at<now())) AND NOT EXISTS(SELECT 1 FROM knowledge_index_jobs j
                WHERE j.generation_id=b.generation_id AND j.tenant_id=b.tenant_id)
              ORDER BY generation_id LIMIT 10'''))).mappings().all()]

    async def claim(self,item,token):
        async with self.session(item) as s:
            r=(await s.execute(text('''UPDATE knowledge_generation_builds SET status='running',lease_token=:token,
              lease_expires_at=now()+interval '180 seconds',attempts=attempts+1,error_code=NULL
              WHERE generation_id=:id AND tenant_id=:tenant AND user_id=:user AND attempts<3
              AND (status='queued' OR (status='running' AND lease_expires_at<now())) RETURNING generation_id'''),
              dict(id=item['generation_id'],tenant=item['tenant_id'],user=item['user_id'],token=token))).first()
            if not r:
                await s.execute(text('''UPDATE knowledge_generation_builds SET status='failed',error_code='GENERATION_RETRY_LIMIT',
                  lease_token=NULL,lease_expires_at=NULL WHERE generation_id=:id AND tenant_id=:tenant AND user_id=:user
                  AND attempts>=3 AND (status='queued' OR (status='running' AND lease_expires_at<now()))'''),
                  dict(id=item['generation_id'],tenant=item['tenant_id'],user=item['user_id']))
            return bool(r)

    async def load(self,item,token):
        async with self.session(item) as s:
            row=(await s.execute(text('''SELECT g.manifest,v.embedding_provider,v.embedding_model,v.embedding_model_version,
              v.embedding_dimensions,v.vector_namespace,v.vector_collection FROM knowledge_generation_builds b
              JOIN knowledge_index_generations g ON g.id=b.generation_id AND g.tenant_id=b.tenant_id
              JOIN knowledge_documents d ON d.id=b.document_id AND d.tenant_id=b.tenant_id
              JOIN knowledge_document_versions v ON v.id=b.version_id AND v.document_id=d.id AND v.tenant_id=d.tenant_id
              WHERE b.generation_id=:id AND b.tenant_id=:tenant AND b.user_id=:user AND d.user_id=:user
              AND b.status='running' AND b.lease_token=:token AND b.lease_expires_at>now() AND g.status='validated'
              AND d.deleted_at IS NULL AND NOT d.is_sensitive AND d.external_embedding_allowed
              AND '''+GENERATION_SOURCE_STATE+''' AND v.content_hash=g.source_hash
              AND (NOT (g.config->>'title_enhancement')::boolean OR g.manifest->>'source_title'=d.title)'''),
              dict(id=item['generation_id'],tenant=item['tenant_id'],user=item['user_id'],token=token))).mappings().first()
            if not row:raise HarnessError('GENERATION_LEASE_OR_SOURCE_LOST','代次租约或来源授权失效')
            points=(await s.execute(text('SELECT chunk_id FROM knowledge_generation_points WHERE generation_id=:id AND tenant_id=:tenant AND user_id=:user'),
                dict(id=item['generation_id'],tenant=item['tenant_id'],user=item['user_id']))).mappings().all()
            return dict(row),{r['chunk_id'] for r in points}

    async def mark(self,item,token,ids,namespace,collection,complete=False):
        async with self.session(item) as s:
            p=dict(id=item['generation_id'],tenant=item['tenant_id'],user=item['user_id'],token=token,namespace=namespace,collection=collection)
            row=(await s.execute(text('''UPDATE knowledge_generation_builds SET lease_expires_at=now()+interval '180 seconds',
              namespace=:namespace,collection=:collection WHERE generation_id=:id AND tenant_id=:tenant AND user_id=:user
              AND lease_token=:token AND status='running' AND lease_expires_at>now()
              AND (namespace IS NULL OR namespace=:namespace) AND (collection IS NULL OR collection=:collection)
              RETURNING generation_id'''),p)).first()
            if not row:raise HarnessError('GENERATION_LEASE_LOST','代次租约失效')
            for cid in ids:
                await s.execute(text('''INSERT INTO knowledge_generation_points(generation_id,chunk_id,tenant_id,user_id)
                  VALUES(:id,:chunk,:tenant,:user) ON CONFLICT DO NOTHING'''),dict(**p,chunk=cid))
            if complete:
                row=(await s.execute(text('''UPDATE knowledge_generation_builds b SET status='ready',lease_token=NULL,lease_expires_at=NULL
                  WHERE generation_id=:id AND tenant_id=:tenant AND user_id=:user AND lease_token=:token
                  AND (SELECT count(*) FROM knowledge_generation_points p WHERE p.generation_id=b.generation_id)=
                      (SELECT count(*) FROM knowledge_generation_chunks c WHERE c.generation_id=b.generation_id)
                  AND EXISTS(SELECT 1 FROM knowledge_documents d JOIN knowledge_document_versions v ON v.id=b.version_id AND v.document_id=d.id AND v.tenant_id=d.tenant_id
                    JOIN knowledge_index_generations g ON g.id=b.generation_id WHERE d.id=b.document_id
                    AND d.tenant_id=:tenant AND d.deleted_at IS NULL AND NOT d.is_sensitive AND d.external_embedding_allowed
                    AND '''+GENERATION_SOURCE_STATE+''' AND v.id=b.version_id AND g.source_hash=v.content_hash) RETURNING generation_id'''),p)).first()
                if not row:raise HarnessError('GENERATION_INCOMPLETE','代次未完整索引或来源已失效')

    async def fail(self,item,token,code):
        async with self.session(item) as s:
            await s.execute(text('''UPDATE knowledge_generation_builds SET status='failed',error_code=:code,lease_token=NULL,
              lease_expires_at=NULL WHERE generation_id=:id AND tenant_id=:tenant AND user_id=:user AND lease_token=:token'''),
              dict(id=item['generation_id'],tenant=item['tenant_id'],user=item['user_id'],token=token,code=code))
