"""起草任务补充回答与草稿 AI 修改接口。"""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException,Request
from app.common.response import success
from app.param.agent_input_param import InputAnswerParam, DraftRevisionParam
from app.service.agent_input_service import AgentInputService
from app.security.dependencies import get_current_user, get_tenant_session
from app.service.run_dispatch_service import get_run_dispatcher_from_request

router=APIRouter(tags=["agent-input"])
service=AgentInputService()


def get_input_service(request:Request):
    return getattr(request.app.state,"agent_input_service",service)


def require_result(result):
    if result is None:
        raise HTTPException(404,"资源不存在或无权访问")
    if result == "conflict":
        raise HTTPException(409,"状态、版本、有效期不允许此操作")
    return result


@router.get("/runs/{run_id}/input-requests")
async def list_inputs(run_id:UUID,user=Depends(get_current_user),session=Depends(get_tenant_session),svc=Depends(get_input_service)):
    return success(await svc.list_inputs(session,user.user,run_id))


@router.get("/input-requests")
async def all_inputs(user=Depends(get_current_user),session=Depends(get_tenant_session),svc=Depends(get_input_service)):
    return success(await svc.list_inputs(session,user.user))


@router.post("/input-requests/{question_id}/answers")
async def answer(question_id:UUID,param:InputAnswerParam,user=Depends(get_current_user),
                 session=Depends(get_tenant_session),dispatcher=Depends(get_run_dispatcher_from_request),svc=Depends(get_input_service)):
    result=require_result(await svc.answer(session,user.user,question_id,param))
    if result["execution_scheduled"]:
        dispatcher.enqueue_after_commit(result["run_id"],resume_input_id=question_id)
    return success(result)


@router.post("/input-requests/{question_id}/cancel")
async def cancel(question_id:UUID,user=Depends(get_current_user),session=Depends(get_tenant_session),svc=Depends(get_input_service)):
    return success(require_result(await svc.cancel(session,user.user,question_id)))


@router.post("/mail/drafts/{draft_id}/revision-requests",status_code=202)
async def revise(draft_id:UUID,param:DraftRevisionParam,user=Depends(get_current_user),
                 session=Depends(get_tenant_session),dispatcher=Depends(get_run_dispatcher_from_request),svc=Depends(get_input_service)):
    result=require_result(await svc.create_revision(session,user.user,draft_id,param))
    if result["status"]=="queued":
        dispatcher.enqueue_after_commit(result["run_id"])
    result["events_url"]=f"/runs/{result['run_id']}/events"
    return success(result)
