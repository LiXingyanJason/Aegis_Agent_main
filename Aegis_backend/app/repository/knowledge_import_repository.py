"""导入、幂等及状态SQL；调用方负责身份隔离事务。"""
from uuid import uuid4
from sqlalchemy import text


class KnowledgeImportRepository:
    async def lock_request(self, session, user, request_id):
        # 同一租户/用户/请求串行化，避免文件写入后并发创建重复文档。
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            dict(key=f"knowledge:{user.tenant_id}:{user.id}:{request_id}"))

    async def replay(self, session, user, request_id):
        return (await session.execute(text("""SELECT d.id AS document_id,v.id AS version_id,j.id AS job_id,
          'uploaded' AS status,d.enabled,d.request_hash,
          CASE WHEN j.id IS NULL THEN 'not_indexed' WHEN j.status='succeeded' AND v.status='ready' THEN 'ready'
            WHEN j.status='succeeded' THEN 'unavailable' ELSE j.status END AS index_status
          FROM knowledge_documents d JOIN knowledge_document_versions v ON v.document_id=d.id AND v.tenant_id=d.tenant_id AND v.version_no=1
          LEFT JOIN knowledge_index_jobs j ON j.version_id=v.id AND j.tenant_id=d.tenant_id
          WHERE d.tenant_id=:tenant AND d.user_id=:user AND d.client_request_id=:request AND d.deleted_at IS NULL"""),
          dict(tenant=user.tenant_id, user=user.id, request=request_id))).mappings().first()

    async def create(self, session, user, metadata, *, storage_key, filename, mime, source_bytes, source_hash, request_hash):
        document, version = uuid4(), uuid4()
        await session.execute(text("""INSERT INTO knowledge_documents
          (id,tenant_id,user_id,title,category,is_sensitive,external_embedding_allowed,client_request_id,request_hash,enabled)
          VALUES(:id,:tenant,:user,:title,:category,:sensitive,:external,:request,:hash,false)"""),
          dict(id=document, tenant=user.tenant_id, user=user.id, title=metadata.title, category=metadata.category,
               sensitive=metadata.is_sensitive,external=metadata.external_embedding_allowed,request=metadata.client_request_id,hash=request_hash))
        # 先按RLS默认私有创建，再由认证所有者显式分享。
        if metadata.visibility == "tenant":
            await session.execute(text("UPDATE knowledge_documents SET visibility='tenant' WHERE id=:id AND tenant_id=:tenant AND user_id=:user"),
                dict(id=document, tenant=user.tenant_id, user=user.id))
        await session.execute(text("""INSERT INTO knowledge_document_versions
          (id,document_id,tenant_id,version_no,version_label,source_text,content_hash,
           storage_backend,storage_key,original_filename,source_bytes,source_mime)
          VALUES(:id,:doc,:tenant,1,'v1',NULL,:hash,'local',:key,:filename,:bytes,:mime)"""),
          dict(id=version, doc=document, tenant=user.tenant_id, hash=source_hash, key=storage_key,
               filename=filename, bytes=source_bytes, mime=mime))
        return dict(document_id=document, version_id=version, job_id=None, status="uploaded",
                    index_status="not_indexed",enabled=False,idempotent_replay=False)

    async def list_documents(self, session, user, limit):
        return (await session.execute(text("""SELECT d.id AS document_id,d.title,d.category,d.visibility,d.created_at,
          v.id AS version_id,j.id AS job_id,d.enabled,d.is_sensitive,d.external_embedding_allowed,
          (d.user_id=:user) AS can_manage,
          CASE WHEN j.id IS NULL THEN 'not_indexed' WHEN j.status='succeeded' AND v.status='ready' THEN 'ready'
            WHEN j.status='succeeded' THEN 'unavailable' ELSE j.status END AS index_status
          FROM knowledge_documents d
          JOIN knowledge_document_versions v ON v.document_id=d.id AND v.tenant_id=d.tenant_id AND v.version_no=1
          LEFT JOIN knowledge_index_jobs j ON j.version_id=v.id AND j.tenant_id=d.tenant_id
          WHERE d.tenant_id=:tenant AND (d.user_id=:user OR d.visibility='tenant') AND d.deleted_at IS NULL
          ORDER BY d.created_at DESC,d.id LIMIT :limit"""),dict(tenant=user.tenant_id,user=user.id,limit=limit))).mappings().all()

    async def job(self, session, user, job_id):
        return (await session.execute(text("""SELECT j.id AS job_id,j.document_id,j.version_id,j.status,j.attempt_count,
          j.total_chunks,j.processed_chunks,j.error_code,j.updated_at FROM knowledge_index_jobs j
          JOIN knowledge_documents d ON d.id=j.document_id AND d.tenant_id=j.tenant_id
          WHERE j.id=:id AND j.tenant_id=:tenant AND j.user_id=:user AND d.user_id=:user AND d.deleted_at IS NULL"""),
          dict(id=job_id,tenant=user.tenant_id,user=user.id))).mappings().first()

    async def owned_source(self, session, user, document_id):
        # 启动/启用以文档行锁串行化；共享读者无权执行这些操作。
        return (await session.execute(text("""SELECT d.id AS document_id,d.enabled,d.is_sensitive,
          d.external_embedding_allowed,d.current_version_id,v.id AS version_id,v.status AS version_status,
          v.embedding_provider,v.embedding_model,v.embedding_model_version,v.embedding_dimensions,
          j.id AS job_id,j.status AS job_status,j.total_chunks,j.processed_chunks
          FROM knowledge_documents d JOIN knowledge_document_versions v ON v.document_id=d.id AND v.tenant_id=d.tenant_id AND v.version_no=1
          LEFT JOIN knowledge_index_jobs j ON j.version_id=v.id AND j.tenant_id=d.tenant_id
          WHERE d.id=:id AND d.tenant_id=:tenant AND d.user_id=:user AND d.deleted_at IS NULL FOR UPDATE OF d"""),
          dict(id=document_id,tenant=user.tenant_id,user=user.id))).mappings().first()

    async def create_index_job(self, session, user, source, profile):
        await session.execute(text("""UPDATE knowledge_documents SET external_embedding_allowed=true,updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND deleted_at IS NULL"""),
          dict(id=source['document_id'],tenant=user.tenant_id,user=user.id))
        await session.execute(text("""UPDATE knowledge_document_versions SET embedding_provider=:provider,
          embedding_model=:model,embedding_model_version=:model_version,embedding_dimensions=:dimensions
          WHERE id=:id AND tenant_id=:tenant"""),dict(id=source['version_id'],tenant=user.tenant_id,**profile))
        job=uuid4()
        await session.execute(text("""INSERT INTO knowledge_index_jobs(id,tenant_id,user_id,document_id,version_id)
          VALUES(:id,:tenant,:user,:doc,:version)"""),dict(id=job,tenant=user.tenant_id,user=user.id,
          doc=source['document_id'],version=source['version_id']))
        return dict(document_id=source['document_id'],version_id=source['version_id'],job_id=job,status='queued',
                    index_status='queued',enabled=source['enabled'],idempotent_replay=False)

    async def activation(self,session,user,document_id,enabled):
        await session.execute(text("""UPDATE knowledge_documents SET enabled=:enabled,updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND deleted_at IS NULL"""),
          dict(enabled=enabled,id=document_id,tenant=user.tenant_id,user=user.id))

    async def retry(self, session, user, job_id, maximum=3):
        # 不新建job，也不清空Embedding累计预算；超过治理额度依然失败。
        row = (await session.execute(text("""UPDATE knowledge_index_jobs j SET status='queued',error_code=NULL,
          lease_token=NULL,lease_expires_at=NULL,last_dispatched_at=NULL,updated_at=now()
          WHERE j.id=:id AND j.tenant_id=:tenant AND j.user_id=:user AND j.status='failed' AND j.attempt_count<:maximum
            AND EXISTS(SELECT 1 FROM knowledge_documents d WHERE d.id=j.document_id AND d.tenant_id=j.tenant_id
              AND d.user_id=:user AND d.deleted_at IS NULL AND NOT d.is_sensitive AND d.external_embedding_allowed)
          RETURNING j.id AS job_id,j.version_id,j.document_id,j.status,'queued' AS index_status,
          false AS idempotent_replay"""),dict(id=job_id,tenant=user.tenant_id,user=user.id,maximum=maximum))).mappings().first()
        if row:
            await session.execute(text("UPDATE knowledge_document_versions SET status='pending' WHERE id=:id AND tenant_id=:tenant"),dict(id=row["version_id"],tenant=user.tenant_id))
        return row
