"""Personal Skill management; mutations never invoke an Agent."""
from uuid import UUID
from fastapi import APIRouter,Depends,Query,HTTPException,Response
from app.common.response import success
from app.security.dependencies import CurrentUser,get_current_user,get_tenant_session
from app.param.skill_param import SkillContent,SkillUpdate,SkillRevision,SkillEnabled
from app.service.skill_service import SkillService

def no_store(response:Response): response.headers['Cache-Control']='no-store'
router=APIRouter(prefix='/skills',tags=['skills'],dependencies=[Depends(no_store)])
service=SkillService()

@router.get('/templates')
async def templates(user:CurrentUser=Depends(get_current_user)):
    return success({'items':[service.template()]})

@router.get('')
async def listing(limit:int=Query(20,ge=1,le=100),cursor:UUID|None=None,
                  user:CurrentUser=Depends(get_current_user),session=Depends(get_tenant_session)):
    rows=await service.repo.list(session,user.user,limit,cursor)
    return success({'items':[dict(row) for row in rows],'next_cursor':rows[-1]['id'] if len(rows)==limit else None})

@router.post('',status_code=201)
async def create(param:SkillContent,user:CurrentUser=Depends(get_current_user),session=Depends(get_tenant_session)):
    return success(await service.create(session,user.user,param))

@router.get('/runs/{run_id}/usages')
async def usages(run_id:UUID,limit:int=Query(50,ge=1,le=100),cursor:UUID|None=None,
                 user:CurrentUser=Depends(get_current_user),session=Depends(get_tenant_session)):
    return success(await service.usages(session,user.user,run_id,limit,cursor))

@router.get('/{skill_id}')
async def detail(skill_id:UUID,user:CurrentUser=Depends(get_current_user),session=Depends(get_tenant_session)):
    return success(dict(await service.owned(session,user.user,skill_id)))

@router.put('/{skill_id}')
async def update(skill_id:UUID,param:SkillUpdate,user:CurrentUser=Depends(get_current_user),session=Depends(get_tenant_session)):
    return success(await service.change(session,user.user,skill_id,param,'edited'))

@router.post('/{skill_id}/publish')
async def publish(skill_id:UUID,param:SkillRevision,user:CurrentUser=Depends(get_current_user),session=Depends(get_tenant_session)):
    return success(await service.change(session,user.user,skill_id,param,'published'))

@router.patch('/{skill_id}/enabled')
async def enabled(skill_id:UUID,param:SkillEnabled,user:CurrentUser=Depends(get_current_user),session=Depends(get_tenant_session)):
    return success(await service.change(session,user.user,skill_id,param,'enabled'))

@router.delete('/{skill_id}')
async def delete(skill_id:UUID,param:SkillRevision,user:CurrentUser=Depends(get_current_user),session=Depends(get_tenant_session)):
    return success(await service.change(session,user.user,skill_id,param,'deleted'))

@router.get('/{skill_id}/versions')
async def versions(skill_id:UUID,limit:int=Query(20,ge=1,le=100),before:int|None=Query(None,ge=1),
                   user:CurrentUser=Depends(get_current_user),session=Depends(get_tenant_session)):
    await service.owned(session,user.user,skill_id)
    rows=await service.repo.versions(session,user.user,skill_id,limit,before)
    return success({'items':[dict(row) for row in rows],'next_before':rows[-1]['version'] if len(rows)==limit else None})

@router.get('/{skill_id}/versions/{version}')
async def version_detail(skill_id:UUID,version:int,user:CurrentUser=Depends(get_current_user),session=Depends(get_tenant_session)):
    await service.owned(session,user.user,skill_id)
    row=await service.repo.version(session,user.user,skill_id,version)
    if row is None: raise HTTPException(404,detail={'code':'SKILL_VERSION_NOT_FOUND'})
    return success(dict(row))
