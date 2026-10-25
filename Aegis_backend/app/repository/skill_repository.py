"""All mutations lock the owned row; releases are insert-only."""
import json
from uuid import uuid4
from sqlalchemy import text

class SkillRepository:
    async def list(self,session,user,limit,cursor):
        return (await session.execute(text('''SELECT id,name,description,purposes,revision,
            published_version,enabled,created_at,updated_at FROM skills
            WHERE tenant_id=:tenant AND user_id=:user AND deleted_at IS NULL
            AND (CAST(:cursor AS uuid) IS NULL OR id>CAST(:cursor AS uuid)) ORDER BY id LIMIT :limit'''),
            dict(tenant=user.tenant_id,user=user.id,cursor=cursor,limit=limit))).mappings().all()

    async def get(self,session,user,skill_id,lock=False):
        return (await session.execute(text('''SELECT * FROM skills WHERE id=:id
            AND tenant_id=:tenant AND user_id=:user AND deleted_at IS NULL'''+(' FOR UPDATE' if lock else '')),
            dict(id=skill_id,tenant=user.tenant_id,user=user.id))).mappings().first()

    async def create(self,session,user,content):
        # Serialize per-user creation to enforce the bounded catalogue under concurrency.
        await session.execute(text('SELECT id FROM users WHERE id=:id AND tenant_id=:tenant FOR UPDATE'),
                              dict(id=user.id,tenant=user.tenant_id))
        count=(await session.execute(text('SELECT count(*) FROM skills WHERE tenant_id=:tenant AND user_id=:user AND deleted_at IS NULL'),
                                     dict(tenant=user.tenant_id,user=user.id))).scalar_one()
        if count>=20: return None
        values=content.model_dump()
        values.update(id=uuid4(),tenant=user.tenant_id,user=user.id,purposes=json.dumps(content.purposes))
        return (await session.execute(text('''INSERT INTO skills(id,tenant_id,user_id,name,description,body,purposes)
            VALUES(:id,:tenant,:user,:name,:description,:body,CAST(:purposes AS jsonb)) RETURNING *'''),values)).mappings().one()

    async def update(self,session,row,content):
        return (await session.execute(text('''UPDATE skills SET name=:name,description=:description,
            body=:body,purposes=CAST(:purposes AS jsonb),revision=revision+1,updated_at=now()
            WHERE id=:id AND tenant_id=:tenant AND user_id=:user RETURNING *'''),
            dict(id=row['id'],tenant=row['tenant_id'],user=row['user_id'],name=content.name,
                 description=content.description,body=content.body,purposes=json.dumps(content.purposes)))).mappings().one()

    async def publish(self,session,row,digest):
        await session.execute(text('''INSERT INTO skill_versions(skill_id,tenant_id,user_id,version,name,description,body,purposes,content_hash)
            VALUES(:id,:tenant_id,:user_id,:revision,:name,:description,:body,CAST(:purposes AS jsonb),:hash)
            ON CONFLICT(skill_id,version) DO NOTHING'''),
            {**row,'purposes':json.dumps(row['purposes']),'hash':digest})
        return (await session.execute(text('''UPDATE skills SET published_version=:revision,enabled=true,
            revision=revision+1,updated_at=now() WHERE id=:id AND tenant_id=:tenant_id AND user_id=:user_id RETURNING *'''),dict(row))).mappings().one()

    async def state(self,session,row,*,enabled=None,delete=False):
        return (await session.execute(text('''UPDATE skills SET enabled=:enabled,
            deleted_at=CASE WHEN :deleted THEN now() ELSE deleted_at END,revision=revision+1,updated_at=now()
            WHERE id=:id AND tenant_id=:tenant_id AND user_id=:user_id RETURNING *'''),
            {**row,'enabled':False if delete else enabled,'deleted':delete})).mappings().one()

    async def event(self,session,row,action):
        await session.execute(text('''INSERT INTO skill_events(id,skill_id,tenant_id,user_id,revision,action)
            VALUES(:event_id,:id,:tenant_id,:user_id,:revision,:action)'''),{**row,'event_id':uuid4(),'action':action})

    async def versions(self,session,user,skill_id,limit,before):
        return (await session.execute(text('''SELECT version,name,description,purposes,content_hash,created_at
            FROM skill_versions WHERE skill_id=:id AND tenant_id=:tenant AND user_id=:user
            AND (CAST(:before AS integer) IS NULL OR version<CAST(:before AS integer)) ORDER BY version DESC LIMIT :limit'''),
            dict(id=skill_id,tenant=user.tenant_id,user=user.id,before=before,limit=limit))).mappings().all()

    async def version(self,session,user,skill_id,version):
        return (await session.execute(text('''SELECT * FROM skill_versions WHERE skill_id=:id AND version=:version
            AND tenant_id=:tenant AND user_id=:user'''),dict(id=skill_id,version=version,tenant=user.tenant_id,user=user.id))).mappings().first()

    async def has_run(self,session,user,run_id):
        return bool((await session.execute(text('SELECT id FROM agent_runs WHERE id=:run AND tenant_id=:tenant AND user_id=:user'),
            dict(run=run_id,tenant=user.tenant_id,user=user.id))).first())

    async def bindings(self,session,user,run_id):
        return (await session.execute(text('''SELECT purpose,status,skill_id,version,content_hash,reads,reason,created_at
            FROM run_skill_bindings WHERE run_id=:run AND tenant_id=:tenant AND user_id=:user ORDER BY purpose'''),
            dict(run=run_id,tenant=user.tenant_id,user=user.id))).mappings().all()

    async def usages(self,session,user,run_id,limit,cursor):
        return (await session.execute(text('''SELECT u.attempt_id,u.purpose,u.skill_id,u.version,u.content_hash,u.loaded_chars,
            u.created_at,a.status,a.prompt_tokens,a.completion_tokens,a.total_tokens,
            EXTRACT(EPOCH FROM (a.finished_at-a.started_at))*1000 AS duration_ms
            FROM run_skill_usages u JOIN run_execution_attempts a ON a.id=u.attempt_id AND a.run_id=u.run_id AND a.tenant_id=u.tenant_id
            WHERE u.run_id=:run AND u.tenant_id=:tenant AND u.user_id=:user
            AND (CAST(:cursor AS uuid) IS NULL OR u.attempt_id>CAST(:cursor AS uuid)) ORDER BY u.attempt_id LIMIT :limit'''),
            dict(run=run_id,tenant=user.tenant_id,user=user.id,limit=limit,cursor=cursor))).mappings().all()
