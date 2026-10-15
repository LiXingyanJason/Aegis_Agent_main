"""资料导入/索引状态入口；不开放已有文档更新、下载或远程URL导入。"""
import json
from uuid import UUID
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import ValidationError
from app.common.response import success
from app.security.dependencies import CurrentUser,get_current_user,get_tenant_session
from app.param.knowledge_param import KnowledgeImportMetadata,KnowledgeTextImportParam,KnowledgeIndexStartParam,KnowledgeActivationParam
from app.vo.knowledge_vo import KnowledgeImportVO,KnowledgeDocumentVO,KnowledgeIndexJobVO,KnowledgeActivationVO
from app.service.knowledge_import_service import KnowledgeImportError

router = APIRouter(prefix='/knowledge',tags=['knowledge'])


def service(request: Request):
    return request.app.state.knowledge_import_service


def dispatcher(request: Request):
    return request.app.state.knowledge_dispatch_service


def error_response(error):
    return HTTPException(status_code=error.status,detail={'code':error.code,'message':str(error)})


@router.post('/documents',status_code=201)
async def import_text(param:KnowledgeTextImportParam,current_user:CurrentUser=Depends(get_current_user),
                      session=Depends(get_tenant_session), imports=Depends(service)):
    """仅保存文本源文件和资料记录，不创建索引任务、不入队、不自动启用。"""
    metadata = KnowledgeImportMetadata.model_validate(param.model_dump(exclude={'content'}))
    try:
        item = await imports.import_source(session,current_user.user,metadata,param.content.encode('utf-8'),'document.txt')
    except KnowledgeImportError as error:
        raise error_response(error) from None
    return success(KnowledgeImportVO(**item))


@router.post('/documents/upload',status_code=201)
async def upload_document(metadata:str=Form(...),file:UploadFile=File(...),
    current_user:CurrentUser=Depends(get_current_user),session=Depends(get_tenant_session),
    imports=Depends(service)):
    """仅上传 Markdown／TXT 源文件；后续索引和启用分别操作。"""
    try:
        param = KnowledgeImportMetadata.model_validate(json.loads(metadata))
    except (ValueError,ValidationError):
        raise HTTPException(422,detail={'code':'KNOWLEDGE_METADATA_INVALID','message':'metadata必须是合法业务参数JSON，不接受路径或身份字段'}) from None
    try:
        if file.content_type and file.content_type.split(';')[0] not in {'text/plain','text/markdown','text/x-markdown','application/octet-stream'}:
            raise KnowledgeImportError('KNOWLEDGE_SOURCE_TYPE_INVALID','仅支持文本文件MIME类型')
        content = await file.read(imports.max_source_bytes+1)
        item = await imports.import_source(session,current_user.user,param,content,file.filename)
    except KnowledgeImportError as error:
        raise error_response(error) from None
    finally:
        await file.close()
    return success(KnowledgeImportVO(**item))


@router.post('/documents/{document_id}/index-jobs',status_code=202)
async def start_index(document_id:UUID,param:KnowledgeIndexStartParam,current_user:CurrentUser=Depends(get_current_user),
    session=Depends(get_tenant_session),imports=Depends(service),dispatch=Depends(dispatcher)):
    """所有者显式确认外发后创建/复用索引任务，提交成功才唤醒Worker。"""
    try:
        item=await imports.start_index(session,current_user.user,document_id,param)
    except KnowledgeImportError as error:
        raise error_response(error) from None
    if item['status']=='queued':
        dispatch.enqueue_after_commit(item,current_user.user)
    return success(KnowledgeImportVO(**item))


@router.patch('/documents/{document_id}/activation')
async def activate_document(document_id:UUID,param:KnowledgeActivationParam,current_user:CurrentUser=Depends(get_current_user),
    session=Depends(get_tenant_session),imports=Depends(service)):
    """所有者控制是否参与检索；禁用不删文件/向量，也不取消独立索引任务。"""
    try:
        item=await imports.activate(session,current_user.user,document_id,param.enabled)
    except KnowledgeImportError as error:
        raise error_response(error) from None
    return success(KnowledgeActivationVO(**item))


@router.get('/documents')
async def list_documents(limit:int=Query(50,ge=1,le=100),current_user:CurrentUser=Depends(get_current_user),
                          session=Depends(get_tenant_session),imports=Depends(service)):
    """查看当前用户有权访问的资料列表"""
    items = await imports.list_documents(session,current_user.user,limit)
    return success([KnowledgeDocumentVO(**item) for item in items])


@router.get('/index-jobs/{job_id}')
async def get_index_job(job_id:UUID,current_user:CurrentUser=Depends(get_current_user),
                        session=Depends(get_tenant_session),imports=Depends(service)):
    """查询一次索引任务的状态和进度"""
    item = await imports.job(session,current_user.user,job_id)
    if item is None:
        raise HTTPException(404,detail='索引任务不存在或无权访问')
    return success(KnowledgeIndexJobVO(**item))


@router.post('/index-jobs/{job_id}/retry',status_code=202)
async def retry_job(job_id:UUID,current_user:CurrentUser=Depends(get_current_user),session=Depends(get_tenant_session),
                    imports=Depends(service),dispatch=Depends(dispatcher)):
    """重新投递允许重试的失败任务"""
    if await imports.job(session,current_user.user,job_id) is None:
        raise HTTPException(404,detail='索引任务不存在或无权访问')
    item = await imports.retry(session,current_user.user,job_id)
    if item is None:
        raise HTTPException(409,detail={'code':'KNOWLEDGE_JOB_NOT_RETRYABLE','message':'仅失败且仍获外发授权的任务可以重试'})
    dispatch.enqueue_after_commit(item,current_user.user)
    return success(KnowledgeImportVO(**item))
