import hashlib
import json
from pathlib import Path
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from app.param.skill_param import SkillContent
from app.repository.skill_repository import SkillRepository

def content_hash(content):
    return hashlib.sha256(json.dumps(content.model_dump(),sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

class SkillService:
    def __init__(self,repository=None): self.repo=repository or SkillRepository()

    async def owned(self,session,user,skill_id,expected=None):
        row=await self.repo.get(session,user,skill_id,lock=expected is not None)
        if row is None: raise HTTPException(404,detail={'code':'SKILL_NOT_FOUND'})
        if expected is not None and row['revision']!=expected:
            raise HTTPException(409,detail={'code':'SKILL_REVISION_CONFLICT','revision':row['revision']})
        return row

    async def create(self,session,user,param):
        try: row=await self.repo.create(session,user,param)
        except IntegrityError: raise HTTPException(409,detail={'code':'SKILL_NAME_CONFLICT'}) from None
        if row is None: raise HTTPException(409,detail={'code':'SKILL_CATALOG_LIMIT'})
        await self.repo.event(session,row,'created')
        return dict(row)

    async def change(self,session,user,skill_id,param,action):
        row=await self.owned(session,user,skill_id,param.expected_revision)
        if action=='edited':
            try: row=await self.repo.update(session,row,param)
            except IntegrityError: raise HTTPException(409,detail={'code':'SKILL_NAME_CONFLICT'}) from None
        elif action=='published':
            content=SkillContent.model_validate({key:row[key] for key in SkillContent.model_fields})
            row=await self.repo.publish(session,row,content_hash(content))
        elif action=='enabled':
            if param.enabled and row['published_version'] is None:
                raise HTTPException(409,detail={'code':'SKILL_NOT_PUBLISHED'})
            row=await self.repo.state(session,row,enabled=param.enabled)
        else: row=await self.repo.state(session,row,delete=True)
        await self.repo.event(session,row,action)
        return dict(row)

    def template(self):
        # Fixed packaged path: no user-controlled path or executable scripts.
        source=(Path(__file__).resolve().parents[1]/'assets/skills/renewal-reply/SKILL.md').read_text(encoding='utf-8')
        _,header,body=source.split('---',2)
        metadata=json.loads(header)
        return SkillContent(**metadata,body=body.strip()).model_dump()

    async def usages(self,session,user,run_id,limit,cursor):
        if not await self.repo.has_run(session,user,run_id):
            raise HTTPException(404,detail={'code':'RUN_NOT_FOUND'})
        rows=await self.repo.usages(session,user,run_id,limit,cursor)
        bindings=await self.repo.bindings(session,user,run_id)
        return {'items':[dict(row) for row in rows],'bindings':[dict(row) for row in bindings],
                'next_cursor':rows[-1]['attempt_id'] if len(rows)==limit else None}
