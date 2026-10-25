"""Owned catalogue, atomic run binding and version revalidation. No prompt persistence."""
from contextlib import asynccontextmanager
from sqlalchemy import text
from aegis_agent_worker.config.database import TenantContext,tenant_transaction

class SkillRepository:
    def __init__(self,database): self.database=database

    @asynccontextmanager
    async def session(self,run):
        async with self.database.session() as session:
            async with tenant_transaction(session,TenantContext(run.tenant_id,run.user_id)):
                yield session

    @staticmethod
    def params(run,purpose):
        return dict(run=run.id,tenant=run.tenant_id,user=run.user_id,purpose=purpose)

    async def claim_selection(self,run,purpose):
        async with self.session(run) as session:
            params=self.params(run,purpose)
            inserted=(await session.execute(text('''INSERT INTO run_skill_bindings(run_id,tenant_id,user_id,purpose,status,reason)
                SELECT id,tenant_id,user_id,:purpose,'selecting','selection_pending' FROM agent_runs
                WHERE id=:run AND tenant_id=:tenant AND user_id=:user AND status='running'
                ON CONFLICT(run_id,purpose) DO NOTHING RETURNING run_id'''),params)).first()
            row=(await session.execute(text('''SELECT * FROM run_skill_bindings WHERE run_id=:run
                AND tenant_id=:tenant AND user_id=:user AND purpose=:purpose'''),params)).mappings().first()
            return bool(inserted),dict(row) if row else None

    async def candidates(self,run,purpose,limit):
        async with self.session(run) as session:
            rows=(await session.execute(text('''SELECT s.id,v.version,v.name,v.description,v.content_hash FROM skills s
                JOIN skill_versions v ON v.skill_id=s.id AND v.version=s.published_version
                  AND v.tenant_id=s.tenant_id AND v.user_id=s.user_id
                WHERE s.tenant_id=:tenant AND s.user_id=:user AND s.enabled AND s.deleted_at IS NULL
                  AND v.purposes @> CAST(:purposes AS jsonb) ORDER BY s.id LIMIT :limit'''),
                {**self.params(run,purpose),'purposes':'["'+purpose+'"]','limit':limit})).mappings().all()
            return [{**row,'id':str(row['id'])} for row in rows]

    async def select(self,run,purpose,selected,reason):
        async with self.session(run) as session:
            await session.execute(text('''UPDATE run_skill_bindings SET status=:status,skill_id=:id,version=:version,
                content_hash=:hash,reason=:reason WHERE run_id=:run AND tenant_id=:tenant AND user_id=:user
                AND purpose=:purpose AND status='selecting' '''),{**self.params(run,purpose),
                'status':'selected' if selected else 'none','id':selected['id'] if selected else None,
                'version':selected['version'] if selected else None,'hash':selected['content_hash'] if selected else None,'reason':reason})

    async def claim_read(self,run,purpose,skill_id):
        async with self.session(run) as session:
            return bool((await session.execute(text('''UPDATE run_skill_bindings SET reads=reads+1
                WHERE run_id=:run AND tenant_id=:tenant AND user_id=:user AND purpose=:purpose
                AND skill_id=:id AND status='selected' AND reads=0 RETURNING run_id'''),
                {**self.params(run,purpose),'id':skill_id})).first())

    async def load(self,run,purpose):
        async with self.session(run) as session:
            row=(await session.execute(text('''SELECT v.*,b.content_hash AS pinned_hash,b.reads FROM run_skill_bindings b
                JOIN skills s ON s.id=b.skill_id AND s.tenant_id=b.tenant_id AND s.user_id=b.user_id
                JOIN skill_versions v ON v.skill_id=b.skill_id AND v.version=b.version
                    AND v.tenant_id=b.tenant_id AND v.user_id=b.user_id
                WHERE b.run_id=:run AND b.tenant_id=:tenant AND b.user_id=:user AND b.purpose=:purpose
                  AND b.status='selected' AND s.enabled AND s.deleted_at IS NULL
                  AND v.purposes @> CAST(:purposes AS jsonb)'''),
                {**self.params(run,purpose),'purposes':'["'+purpose+'"]'})).mappings().first()
            return dict(row) if row else None

    async def record(self,run,purpose,attempt_id,row):
        async with self.session(run) as session:
            await session.execute(text('''INSERT INTO run_skill_usages(attempt_id,run_id,tenant_id,user_id,purpose,
                skill_id,version,content_hash,loaded_chars)
                VALUES(:attempt,:run,:tenant,:user,:purpose,:skill,:version,:hash,:chars)'''),
                {**self.params(run,purpose),'attempt':attempt_id,'skill':row['skill_id'],
                 'version':row['version'],'hash':row['content_hash'],'chars':len(row['body'])})
