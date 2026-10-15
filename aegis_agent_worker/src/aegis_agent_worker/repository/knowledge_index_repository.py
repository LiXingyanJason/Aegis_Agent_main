"""索引领取租约、片段和进度SQL；不调用模型/文件/向量库。"""
from contextlib import asynccontextmanager
from sqlalchemy import text
from aegis_agent_worker.config.database import TenantContext,tenant_transaction
from aegis_agent_worker.harness.exceptions import HarnessError


class KnowledgeIndexRepository:
    def __init__(self,database):
        self._database=database

    @asynccontextmanager
    async def session(self,message):
        async with self._database.session() as session:
            async with tenant_transaction(session,TenantContext(message['tenant_id'],message['user_id'])):
                yield session

    async def recoverable(self,interval,limit=50):
        # 仅调度身份元数据：专用Index Worker需要跨租户任务调度权限；内容读写随后回到租户事务。
        async with self._database.session() as session:
            async with session.begin():
                rows=(await session.execute(text("""UPDATE knowledge_index_jobs SET last_dispatched_at=now()
                  WHERE id IN (SELECT id FROM knowledge_index_jobs
                    WHERE (status='queued' OR (status='running' AND lease_expires_at<now()))
                      AND EXISTS(SELECT 1 FROM knowledge_documents d WHERE d.id=knowledge_index_jobs.document_id
                        AND d.tenant_id=knowledge_index_jobs.tenant_id AND d.deleted_at IS NULL)
                      AND (last_dispatched_at IS NULL OR last_dispatched_at<now()-make_interval(secs=>:interval))
                    ORDER BY last_dispatched_at NULLS FIRST,created_at LIMIT :limit FOR UPDATE SKIP LOCKED)
                  RETURNING id AS job_id,tenant_id,user_id"""),dict(interval=interval,limit=limit))).mappings().all()
                return [dict(row) for row in rows]

    async def claim(self,message,token,seconds,maximum):
        params=dict(id=message['job_id'],tenant=message['tenant_id'],user=message['user_id'],token=token,
                    seconds=seconds,maximum=maximum)
        async with self.session(message) as session:
            row=(await session.execute(text("""UPDATE knowledge_index_jobs j SET status='running',
              attempt_count=attempt_count+1,lease_token=:token,lease_expires_at=now()+make_interval(secs=>:seconds),
              error_code=NULL,updated_at=now()
              WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND attempt_count<:maximum
                AND (status='queued' OR (status='running' AND lease_expires_at<now()))
                AND EXISTS(SELECT 1 FROM knowledge_documents d WHERE d.id=j.document_id AND d.tenant_id=j.tenant_id
                    AND d.user_id=:user AND d.deleted_at IS NULL)
                AND EXISTS(SELECT 1 FROM knowledge_document_versions v WHERE v.id=j.version_id AND v.tenant_id=j.tenant_id
                    AND v.status IN ('pending','indexing','failed'))
              RETURNING j.*"""),params)).mappings().first()
            if not row:
                exhausted=(await session.execute(text("""UPDATE knowledge_index_jobs SET status='failed',
                    error_code='KNOWLEDGE_RETRY_LIMIT',lease_token=NULL,lease_expires_at=NULL,updated_at=now()
                    WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND attempt_count>=:maximum
                      AND (status='queued' OR (status='running' AND lease_expires_at<now())) RETURNING version_id"""),params)).first()
                if exhausted:
                    await session.execute(text("UPDATE knowledge_document_versions SET status='failed' WHERE id=:id AND tenant_id=:tenant"),dict(id=exhausted[0],tenant=message['tenant_id']))
                return None
            await session.execute(text("UPDATE knowledge_document_versions SET status='indexing' WHERE id=:id AND tenant_id=:tenant AND status IN ('pending','indexing','failed')"),dict(id=row['version_id'],tenant=message['tenant_id']))
            return dict(row)

    async def load(self,message,token):
        async with self.session(message) as session:
            row=(await session.execute(text("""SELECT j.*,v.source_text,v.storage_backend,v.storage_key,
              v.original_filename,v.source_bytes,v.content_hash AS source_hash,v.embedding_provider,v.embedding_model,
              v.embedding_model_version,v.embedding_dimensions,d.category,d.visibility,d.is_sensitive,d.external_embedding_allowed
              FROM knowledge_index_jobs j JOIN knowledge_documents d ON d.id=j.document_id AND d.tenant_id=j.tenant_id
              JOIN knowledge_document_versions v ON v.id=j.version_id AND v.document_id=d.id AND v.tenant_id=d.tenant_id
              WHERE j.id=:id AND j.tenant_id=:tenant AND j.user_id=:user AND d.user_id=:user
                AND d.deleted_at IS NULL AND j.status='running' AND j.lease_token=:token AND j.lease_expires_at>now()"""),
              dict(id=message['job_id'],tenant=message['tenant_id'],user=message['user_id'],token=token))).mappings().first()
        if not row:
            raise HarnessError('KNOWLEDGE_LEASE_LOST','索引任务已被取消或租约失效')
        return dict(row)

    async def renew(self,message,token,seconds):
        async with self.session(message) as session:
            row=(await session.execute(text("""UPDATE knowledge_index_jobs SET lease_expires_at=now()+make_interval(secs=>:seconds),updated_at=now()
                WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND lease_token=:token AND status='running'
                  AND lease_expires_at>now() RETURNING id"""),
                dict(id=message['job_id'],tenant=message['tenant_id'],user=message['user_id'],token=token,seconds=seconds))).first()
            if not row: raise HarnessError('KNOWLEDGE_LEASE_LOST','索引租约已失效')

    async def bind_space(self,namespace,collection,profile,remote_count):
        # 全局基础设施模型映射不是用户正文；不放在Agent State或API响应中。
        async with self._database.session() as session:
            async with session.begin():
                await session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),dict(key='vector-space:'+namespace))
                row=(await session.execute(text('SELECT * FROM knowledge_vector_spaces WHERE namespace=:namespace'),dict(namespace=namespace))).mappings().first()
                expected=(profile.provider,profile.model,profile.version,profile.dimensions)
                if row:
                    if (row['model_provider'],row['model_name'],row['model_version'],row['dimensions'])!=expected:
                        raise HarnessError('QDRANT_PROFILE_MISMATCH','集合已绑定不同模型空间')
                else:
                    if type(remote_count) is not int or remote_count!=0:
                        raise HarnessError('QDRANT_COLLECTION_UNBOUND','非空集合未绑定本项目模型，不自动接管')
                    await session.execute(text("""INSERT INTO knowledge_vector_spaces
                      (namespace,collection_name,model_provider,model_name,model_version,dimensions)
                      VALUES(:namespace,:collection,:provider,:model,:version,:dimensions)"""),
                      dict(namespace=namespace,collection=collection,provider=profile.provider,model=profile.model,version=profile.version,dimensions=profile.dimensions))

    async def prepare_chunks(self,message,token,job,chunks,namespace,collection,parser):
        async with self.session(message) as session:
            pinned=(await session.execute(text("""UPDATE knowledge_index_jobs SET vector_namespace=:namespace,
              vector_collection=:collection,parser_version=:parser,total_chunks=:total,updated_at=now()
              WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND lease_token=:token AND status='running'
                AND lease_expires_at>now() AND (vector_namespace IS NULL OR vector_namespace=:namespace)
                AND (parser_version IS NULL OR parser_version=:parser) RETURNING id"""),dict(
              namespace=namespace,collection=collection,parser=parser,total=len(chunks),id=message['job_id'],
              tenant=message['tenant_id'],user=message['user_id'],token=token))).first()
            if not pinned: raise HarnessError('KNOWLEDGE_INDEX_PROFILE_CHANGED','索引目标/解析版本变化或租约失效')
            for chunk in chunks:
                await session.execute(text("""INSERT INTO knowledge_chunks
                  (id,document_id,version_id,tenant_id,position,section,content,start_offset,end_offset,content_hash,
                   embedding_provider,embedding_model,embedding_model_version,embedding_dimensions,search_tokens)
                  VALUES(:id,:doc,:version,:tenant,:position,:section,:content,:start,:end,:hash,:provider,:model,:model_version,:dimensions,:tokens)
                  ON CONFLICT(version_id,position) DO NOTHING"""),dict(id=chunk.id,doc=job['document_id'],version=job['version_id'],
                  tenant=message['tenant_id'],position=chunk.position,section=chunk.section,content=chunk.content,
                  start=chunk.start_offset,end=chunk.end_offset,hash=chunk.content_hash,tokens=chunk.search_tokens,
                  provider=job['embedding_provider'],model=job['embedding_model'],model_version=job['embedding_model_version'],dimensions=job['embedding_dimensions']))
            rows=(await session.execute(text('SELECT id,content_hash,indexed_at FROM knowledge_chunks WHERE version_id=:version AND tenant_id=:tenant ORDER BY position'),dict(version=job['version_id'],tenant=message['tenant_id']))).mappings().all()
            if len(rows)!=len(chunks) or any(row['id']!=chunk.id or row['content_hash']!=chunk.content_hash for row,chunk in zip(rows,chunks)):
                raise HarnessError('KNOWLEDGE_CHUNK_MISMATCH','已有片段与重试解析结果不一致')
            return {row['id'] for row in rows if row['indexed_at'] is not None}

    async def mark_batch(self,message,token,job,ids):
        async with self.session(message) as session:
            alive=(await session.execute(text("""SELECT id FROM knowledge_index_jobs WHERE id=:id AND tenant_id=:tenant
              AND user_id=:user AND lease_token=:token AND status='running' AND lease_expires_at>now() FOR UPDATE"""),
              dict(id=message['job_id'],tenant=message['tenant_id'],user=message['user_id'],token=token))).first()
            if not alive: raise HarnessError('KNOWLEDGE_LEASE_LOST','索引租约失效')
            await session.execute(text("UPDATE knowledge_chunks SET indexed_at=now() WHERE id=ANY(CAST(:ids AS uuid[])) AND version_id=:version AND tenant_id=:tenant"),dict(ids=ids,version=job['version_id'],tenant=message['tenant_id']))
            await session.execute(text("""UPDATE knowledge_index_jobs SET processed_chunks=(SELECT count(*) FROM knowledge_chunks
              WHERE version_id=:version AND tenant_id=:tenant AND indexed_at IS NOT NULL),updated_at=now() WHERE id=:id AND lease_token=:token"""),
              dict(version=job['version_id'],tenant=message['tenant_id'],id=message['job_id'],token=token))

    async def complete(self,message,token,job):
        async with self.session(message) as session:
            row=(await session.execute(text("""UPDATE knowledge_index_jobs j SET status='succeeded',error_code=NULL,
              lease_token=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=:id AND tenant_id=:tenant AND user_id=:user
              AND lease_token=:token AND status='running' AND lease_expires_at>now() AND total_chunks>0 AND processed_chunks=total_chunks
              AND EXISTS(SELECT 1 FROM knowledge_documents d WHERE d.id=j.document_id AND d.tenant_id=j.tenant_id
                AND d.user_id=:user AND d.deleted_at IS NULL AND NOT d.is_sensitive AND d.external_embedding_allowed)
              RETURNING id"""),dict(id=message['job_id'],tenant=message['tenant_id'],user=message['user_id'],token=token))).first()
            if not row: raise HarnessError('KNOWLEDGE_INDEX_NOT_COMPLETE','片段未全部确认、权限失效或租约失效')
            await session.execute(text("UPDATE knowledge_document_versions SET status='ready' WHERE id=:id AND tenant_id=:tenant"),dict(id=job['version_id'],tenant=message['tenant_id']))
            await session.execute(text("""UPDATE knowledge_documents SET current_version_id=:version,updated_at=now()
              WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND deleted_at IS NULL AND current_version_id IS NULL"""),
              dict(version=job['version_id'],id=job['document_id'],tenant=message['tenant_id'],user=message['user_id']))

    async def fail(self,message,token,job,code):
        async with self.session(message) as session:
            row=(await session.execute(text("""UPDATE knowledge_index_jobs SET status='failed',error_code=:code,
              lease_token=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=:id AND tenant_id=:tenant AND user_id=:user
              AND lease_token=:token AND status='running' RETURNING id"""),dict(code=code,id=message['job_id'],tenant=message['tenant_id'],user=message['user_id'],token=token))).first()
            if row:
                await session.execute(text("UPDATE knowledge_document_versions SET status='failed' WHERE id=:id AND tenant_id=:tenant"),dict(id=job['version_id'],tenant=message['tenant_id']))
