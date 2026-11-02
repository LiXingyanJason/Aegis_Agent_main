from sqlalchemy import text


class GenerationPublicationRepository:
    async def publish(self,session,user,document_id,param):
        return (await session.execute(text('''SELECT * FROM aegis_publish_generation(:tenant,:user,:doc,:version,
          :target,:revision,:review,:rollback)'''),dict(tenant=user.tenant_id,user=user.id,doc=document_id,
          version=param.version_id,target=param.generation_id,revision=param.expected_revision,review=param.review_reference,
          rollback=param.rollback))).mappings().one()

    async def status(self,session,user,document_id):
        row=(await session.execute(text('''SELECT v.id AS version_id,j.id AS job_id,j.generation_id AS built_generation_id,
          b.status AS generation_status,coalesce(h.revision,0) AS revision,h.generation_id AS published_generation_id
          FROM knowledge_documents d JOIN knowledge_document_versions v ON v.document_id=d.id AND v.tenant_id=d.tenant_id AND v.version_no=1
          LEFT JOIN knowledge_index_jobs j ON j.version_id=v.id AND j.tenant_id=d.tenant_id
          LEFT JOIN knowledge_generation_builds b ON b.generation_id=j.generation_id AND b.tenant_id=d.tenant_id
          LEFT JOIN knowledge_generation_publications h ON h.document_id=d.id AND h.version_id=v.id AND h.tenant_id=d.tenant_id
          WHERE d.id=:doc AND d.tenant_id=:tenant AND d.user_id=:user AND d.deleted_at IS NULL'''),
          dict(doc=document_id,tenant=user.tenant_id,user=user.id))).mappings().first()
        return dict(row) if row else None
