from uuid import uuid4
from sqlalchemy import text
from aegis_agent_worker.repository.shadow_generation_repository import ShadowGenerationRepository
from aegis_agent_worker.harness.exceptions import HarnessError


class GenerationReleaseService:
    def __init__(self):self.repo=ShadowGenerationRepository()

    async def enqueue(self,session,user,doc,version,generation,retry=False):
        await self.repo.owned(session,user,doc,version)
        p=dict(tenant=user.tenant_id,user=user.id,doc=doc,version=version,id=generation)
        if retry:
            row=(await session.execute(text('''UPDATE knowledge_generation_builds SET status='queued',error_code=NULL
              WHERE generation_id=:id AND tenant_id=:tenant AND user_id=:user AND document_id=:doc AND version_id=:version
                AND status='failed' AND attempts<3 RETURNING generation_id'''),p)).first()
        else:
            row=(await session.execute(text('''INSERT INTO knowledge_generation_builds(generation_id,tenant_id,user_id,document_id,version_id)
              SELECT id,tenant_id,user_id,document_id,version_id FROM knowledge_index_generations
              WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND document_id=:doc AND version_id=:version
                AND status='validated' AND manifest->'chunks'->0 ? 'search_tokens'
              ON CONFLICT DO NOTHING RETURNING generation_id'''),p)).first()
        if not row:raise HarnessError('GENERATION_BUILD_CONFLICT','代次未复核、旧manifest缺少检索词项或已存在任务／不可重试')
        return dict(generation_id=generation,status='queued')

    async def publish(self,session,user,doc,version,generation,expected_revision,review,rollback=False):
        if type(expected_revision) is not int or expected_revision<0 or not isinstance(review,str) or not review.strip() or len(review)>200:
            raise ValueError('发布必须提供revision和评测／回滚审查记录')
        source=await self.repo.owned(session,user,doc,version)
        p=dict(tenant=user.tenant_id,user=user.id,doc=doc,version=version,target=generation)
        if generation is None and not rollback:raise ValueError('清空仅用于明确回滚')
        if generation is not None:
            row=(await session.execute(text('''SELECT b.generation_id FROM knowledge_generation_builds b
              JOIN knowledge_index_generations g ON g.id=b.generation_id AND g.tenant_id=b.tenant_id
              JOIN knowledge_document_versions v ON v.id=b.version_id AND v.document_id=b.document_id AND v.tenant_id=b.tenant_id
              WHERE b.generation_id=:target AND b.tenant_id=:tenant AND b.user_id=:user AND b.document_id=:doc
                AND b.version_id=:version AND b.status='ready' AND g.status='validated' AND g.source_hash=:source_hash
                AND b.namespace=v.vector_namespace AND b.collection=v.vector_collection
                AND (NOT (g.config->>'title_enhancement')::boolean OR g.manifest->>'source_title'=:title)
                AND (SELECT count(*) FROM knowledge_generation_points p WHERE p.generation_id=b.generation_id)=
                    (SELECT count(*) FROM knowledge_generation_chunks c WHERE c.generation_id=b.generation_id)'''),dict(**p,source_hash=source['source_hash'],title=source['title']))).first()
            if not row:raise HarnessError('GENERATION_NOT_READY','代次未完整构建或模型空间／来源已变')
            if rollback:
                prior=(await session.execute(text('''SELECT id FROM knowledge_generation_releases WHERE tenant_id=:tenant
                  AND document_id=:doc AND version_id=:version AND generation_id=:target LIMIT 1'''),p)).first()
                if not prior:raise HarnessError('GENERATION_NOT_PREVIOUS','回滚目标从未发布')
        await session.execute(text('''INSERT INTO knowledge_generation_publications(tenant_id,user_id,document_id,version_id)
          VALUES(:tenant,:user,:doc,:version) ON CONFLICT DO NOTHING'''),p)
        head=(await session.execute(text('''SELECT revision,generation_id FROM knowledge_generation_publications
          WHERE tenant_id=:tenant AND document_id=:doc AND version_id=:version FOR UPDATE'''),p)).mappings().first()
        if head['revision']!=expected_revision:raise HarnessError('GENERATION_REVISION_CONFLICT','发布指针已变化，请刷新')
        if head['generation_id']==generation:return dict(generation_id=generation,revision=head['revision'],changed=False)
        revision=head['revision']+1
        await session.execute(text('''UPDATE knowledge_generation_publications SET generation_id=:target,revision=:revision
          WHERE tenant_id=:tenant AND user_id=:user AND document_id=:doc AND version_id=:version'''),dict(**p,revision=revision))
        await session.execute(text('''INSERT INTO knowledge_generation_releases(tenant_id,user_id,document_id,version_id,revision,
          old_generation_id,generation_id,review_reference) VALUES(:tenant,:user,:doc,:version,:revision,:old,:target,:review)'''),
          dict(**p,revision=revision,old=head['generation_id'],review=review))
        return dict(generation_id=generation,revision=revision,changed=True)
