"""Control-plane shadow generations; caller supplies an owned tenant transaction.

Selection is intentionally NOT the production retrieval pointer. No method here
modifies knowledge_chunks, current_version_id, vector spaces or existing jobs.
"""
import json
from uuid import uuid4
from sqlalchemy import text
from aegis_agent_worker.harness.exceptions import HarnessError


class ShadowGenerationRepository:
    async def lock_request(self,session,user,request_id):
        await session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),
            dict(key=f'shadow:{user.tenant_id}:{user.id}:{request_id}'))

    @staticmethod
    def params(user,document_id,version_id):
        return dict(tenant=user.tenant_id,user=user.id,doc=document_id,version=version_id)

    async def owned(self,session,user,document_id,version_id):
        row=(await session.execute(text('''SELECT d.title,v.content_hash AS source_hash,v.status
          FROM knowledge_documents d JOIN knowledge_document_versions v ON v.document_id=d.id AND v.tenant_id=d.tenant_id
          WHERE d.id=:doc AND v.id=:version AND d.tenant_id=:tenant AND d.user_id=:user
            AND d.deleted_at IS NULL AND d.current_version_id=v.id AND v.status='ready'
            AND NOT d.is_sensitive AND d.external_embedding_allowed FOR UPDATE OF d'''),
          self.params(user,document_id,version_id))).mappings().first()
        if row is None: raise HarnessError('SHADOW_SOURCE_UNAVAILABLE','来源不是当前有效且获授权的所有者资料')
        return dict(row)

    async def existing(self,session,user,request_id):
        return (await session.execute(text('''SELECT * FROM knowledge_index_generations
          WHERE tenant_id=:tenant AND user_id=:user AND request_id=:request'''),
          dict(tenant=user.tenant_id,user=user.id,request=request_id))).mappings().first()

    async def save(self,session,user,document_id,version_id,request_id,request_hash,manifest):
        p=self.params(user,document_id,version_id)
        gid=manifest['scope']['generation_id']
        await session.execute(text('''INSERT INTO knowledge_index_generations(id,tenant_id,user_id,document_id,version_id,
          request_id,request_hash,source_hash,parser_version,config,manifest,manifest_hash)
          VALUES(:id,:tenant,:user,:doc,:version,:request,:request_hash,:source_hash,:parser,CAST(:config AS jsonb),CAST(:manifest AS jsonb),:hash)'''),
          dict(**p,id=gid,request=request_id,request_hash=request_hash,source_hash=manifest['source_hash'],
               parser=manifest['scope']['parser_version'],config=json.dumps(manifest['config']),
               manifest=json.dumps(manifest,ensure_ascii=False),hash=manifest['manifest_hash']))
        for c in manifest['chunks']:
            await session.execute(text('''INSERT INTO knowledge_generation_chunks(generation_id,tenant_id,user_id,
              document_id,version_id,chunk_id,position,payload)
              VALUES(:id,:tenant,:user,:doc,:version,:chunk,:position,CAST(:payload AS jsonb))'''),
              dict(**p,id=gid,chunk=c['chunk_id'],position=c['position'],payload=json.dumps(c,ensure_ascii=False)))
        return gid

    async def validate(self,session,user,document_id,version_id,generation_id,expected_hash,reference):
        source=await self.owned(session,user,document_id,version_id)
        from aegis_agent_worker.indexing.generation import digest
        p=dict(**self.params(user,document_id,version_id),id=generation_id)
        generation=(await session.execute(text('''SELECT manifest,source_hash FROM knowledge_index_generations
          WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND document_id=:doc AND version_id=:version
          FOR UPDATE'''),p)).mappings().first()
        if generation is None: raise HarnessError('SHADOW_VALIDATION_CONFLICT','代次不存在')
        if generation['source_hash']!=source['source_hash']:
            raise HarnessError('SHADOW_SOURCE_CHANGED','来源hash已变化')
        manifest=generation['manifest']
        if isinstance(manifest,str): manifest=json.loads(manifest)
        body={k:v for k,v in manifest.items() if k!='manifest_hash'}
        rows=(await session.execute(text('''SELECT payload FROM knowledge_generation_chunks WHERE generation_id=:id
          AND tenant_id=:tenant AND user_id=:user AND document_id=:doc AND version_id=:version ORDER BY position'''),p)).mappings().all()
        payloads=[json.loads(r['payload']) if isinstance(r['payload'],str) else r['payload'] for r in rows]
        if digest(body)!=expected_hash or manifest['manifest_hash']!=expected_hash or payloads!=manifest['chunks']:
            raise HarnessError('SHADOW_MANIFEST_CHANGED','代次manifest或片段集合校验失败')
        row=(await session.execute(text('''UPDATE knowledge_index_generations SET status='validated',
          validation_hash=:hash,validation_reference=:reference WHERE id=:id AND tenant_id=:tenant AND user_id=:user
          AND document_id=:doc AND version_id=:version AND manifest_hash=:hash AND status='prepared'
          AND EXISTS(SELECT 1 FROM knowledge_generation_chunks c WHERE c.generation_id=knowledge_index_generations.id)
          RETURNING id'''),dict(**self.params(user,document_id,version_id),id=generation_id,hash=expected_hash,reference=reference))).first()
        if not row: raise HarnessError('SHADOW_VALIDATION_CONFLICT','代次不存在、已复核或manifest不匹配')

    async def select(self,session,user,document_id,version_id,target,expected_revision,*,rollback=False):
        source=await self.owned(session,user,document_id,version_id)
        p=self.params(user,document_id,version_id)
        await session.execute(text('''INSERT INTO knowledge_generation_heads(tenant_id,user_id,document_id,version_id)
          VALUES(:tenant,:user,:doc,:version) ON CONFLICT DO NOTHING'''),p)
        head=(await session.execute(text('''SELECT revision,selected_generation_id FROM knowledge_generation_heads
          WHERE tenant_id=:tenant AND user_id=:user AND document_id=:doc AND version_id=:version FOR UPDATE'''),p)).mappings().first()
        if head['revision']!=expected_revision:
            raise HarnessError('SHADOW_REVISION_CONFLICT','影子代次选择已变化，请刷新')
        if target is None and not rollback:
            raise HarnessError('SHADOW_TARGET_REQUIRED','仅回滚允许清空影子指针')
        if target is not None:
            valid=(await session.execute(text('''SELECT id FROM knowledge_index_generations WHERE id=:target
              AND tenant_id=:tenant AND user_id=:user AND document_id=:doc AND version_id=:version
              AND status='validated' AND validation_hash=manifest_hash AND source_hash=:source_hash'''),dict(**p,target=target,source_hash=source['source_hash']))).first()
            if not valid: raise HarnessError('SHADOW_NOT_VALIDATED','影子代次未复核，不能选择')
        if rollback and target is not None:
            prior=(await session.execute(text('''SELECT id FROM knowledge_generation_events WHERE tenant_id=:tenant
              AND user_id=:user AND document_id=:doc AND version_id=:version AND to_generation_id=:target LIMIT 1'''),dict(**p,target=target))).first()
            if not prior: raise HarnessError('SHADOW_NOT_PREVIOUS','回滚目标从未被选择')
        if head['selected_generation_id']==target:
            return dict(generation_id=target,revision=head['revision'],changed=False)
        revision=head['revision']+1
        await session.execute(text('''UPDATE knowledge_generation_heads SET selected_generation_id=:target,revision=:revision
          WHERE tenant_id=:tenant AND user_id=:user AND document_id=:doc AND version_id=:version'''),dict(**p,target=target,revision=revision))
        await session.execute(text('''INSERT INTO knowledge_generation_events(id,tenant_id,user_id,document_id,version_id,
          revision,from_generation_id,to_generation_id,action) VALUES(:id,:tenant,:user,:doc,:version,:revision,:old,:target,:action)'''),
          dict(**p,id=uuid4(),revision=revision,old=head['selected_generation_id'],target=target,action='rollback' if rollback else 'select'))
        return dict(generation_id=target,revision=revision,changed=True)

    async def snapshot(self,session,user,document_id,version_id,generation_id):
        # Explicit historical shadow generation reads require current source access
        # again. No implicit fallback to a different generation or stale excerpt.
        source=await self.owned(session,user,document_id,version_id)
        row=(await session.execute(text('''SELECT manifest,manifest_hash,status FROM knowledge_index_generations
          WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND document_id=:doc AND version_id=:version
            AND source_hash=:source_hash'''),
          dict(**self.params(user,document_id,version_id),id=generation_id,source_hash=source['source_hash']))).mappings().first()
        if row is None: raise HarnessError('SHADOW_GENERATION_UNAVAILABLE','影子代次不存在或不可访问')
        return dict(row)
