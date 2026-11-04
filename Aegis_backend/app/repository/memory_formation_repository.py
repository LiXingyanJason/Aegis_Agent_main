from sqlalchemy import text
from fastapi import HTTPException
import json


class MemoryFormationRepository:
    """Settings only: extraction and automatic approval remain disabled until 6.3/6.4."""
    async def read(self,session,user):
        row=(await session.execute(text('SELECT * FROM memory_formation_settings WHERE tenant_id=:tenant AND user_id=:user'),
            dict(tenant=user.tenant_id,user=user.id))).mappings().first()
        if row:
            return {k:v for k,v in dict(row).items() if k not in ('tenant_id','user_id')}
        return dict(version=0,mode='suggest',use_existing=True,external_processing_allowed=False,
            auto_approval_consent=False,allowed_categories=['preference'],excluded_conversation_ids=[])

    async def update(self,session,user,param):
        p=dict(tenant=user.tenant_id,user=user.id)
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            dict(key=f'memory:{user.tenant_id}:{user.id}'))
        if param.excluded_conversation_ids:
            ids=(await session.execute(text('SELECT id FROM conversations WHERE tenant_id=:tenant AND user_id=:user AND id=ANY(CAST(:ids AS uuid[]))'),
                dict(**p,ids=param.excluded_conversation_ids))).mappings().all()
            if {r['id'] for r in ids}!=set(param.excluded_conversation_ids):
                raise HTTPException(422,detail={'code':'MEMORY_FORMATION_SCOPE_INVALID'})
        current=await self.read(session,user)
        if current['version']!=param.expected_version:
            raise HTTPException(409,detail={'code':'MEMORY_FORMATION_VERSION_CONFLICT'})
        await session.execute(text('''INSERT INTO memory_formation_settings(tenant_id,user_id) VALUES(:tenant,:user)
            ON CONFLICT DO NOTHING'''),p)
        await session.execute(text('''UPDATE memory_formation_settings SET version=version+1,mode=:mode,
            use_existing=:use_existing,external_processing_allowed=:external_processing_allowed,
            auto_approval_consent=:auto_approval_consent,allowed_categories=CAST(:categories AS jsonb),
            excluded_conversation_ids=CAST(:excluded AS jsonb),updated_at=now() WHERE tenant_id=:tenant AND user_id=:user'''),
            dict(**p,**param.model_dump(exclude={'expected_version','allowed_categories','excluded_conversation_ids'}),
                categories=json.dumps(param.allowed_categories),excluded=json.dumps([str(i) for i in param.excluded_conversation_ids])))
        await session.execute(text('''INSERT INTO memory_lifecycle_events(tenant_id,user_id,event_type,object_id,object_version,reason)
            VALUES(:tenant,:user,'settings_changed',:user,:version,'user_settings')'''),dict(**p,version=current['version']+1))
        return await self.read(session,user)
