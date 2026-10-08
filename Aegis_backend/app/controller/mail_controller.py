"""邮件管理 HTTP 接口。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.response import success
from app.param.mail_draft_update_param import MailDraftUpdateParam
from app.param.mail_extraction_request_param import MailExtractionRequestParam
from app.param.mail_reply_draft_request_param import MailReplyDraftRequestParam
from app.param.todo_draft_create_param import TodoDraftCreateParam
from app.security.dependencies import CurrentUser, get_current_user, get_tenant_session
from app.service.mail_service import (
    MailConnectionRequiredError,
    MailListSnapshot,
    MailService,
    MailToolUnavailableError,
)
from app.vo.mail_vo import (
    MailDraftDetailVO,
    MailDraftListItemVO,
    MailMessageVO,
    MailMessagesVO,
    MailAsyncRequestVO,
    SentMailMessageVO,
    TodoDraftVO,
    MailSendConfirmationVO,
    MailAddressVO,
    MailAttachmentVO,
    MailMessageDetailVO,
)
from app.service.run_dispatch_service import RunDispatchService, get_run_dispatcher_from_request


router = APIRouter(prefix="/mail", tags=["mail"])


def get_mail_service_from_request(request: Request) -> MailService:
    """取得应用启动期创建的 Email MCP 应用服务。"""
    return request.app.state.mail_service


@router.get("/messages")
async def list_messages(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
    mail_service: MailService = Depends(get_mail_service_from_request),
) -> dict[str, MailMessagesVO]:
    """同步并返回当前登录用户全部邮件元数据，不下发邮件正文。"""
    try:
        snapshot = await mail_service.sync_and_list_messages(session, current_user.user)
    except MailConnectionRequiredError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "CONNECTION_REQUIRED",
                "message": str(error),
                "required_scopes": ["mail.read"],
            },
        ) from error
    except MailToolUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "MAIL_TOOL_UNAVAILABLE", "message": str(error)},
        ) from error
    return success(_to_messages_vo(snapshot))


def _to_messages_vo(snapshot: MailListSnapshot) -> MailMessagesVO:
    """将仓储行转换为不含正文的页面响应模型。"""
    return MailMessagesVO(
        snapshot_at=snapshot.snapshot_at,
        items=[
            MailMessageVO(
                email_id=item["id"],
                thread_id=item["provider_thread_id"],
                customer=item["sender_name"],
                from_email=item["sender_email"],
                subject=item["subject"],
                received_at=item["received_at"],
                source_version=item["source_version"],
                extraction_status=item["extraction_status"],
            )
            for item in snapshot.items
        ],
    )


@router.get("/messages/{email_id}")
async def get_message_detail(
    email_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
    mail_service: MailService = Depends(get_mail_service_from_request),
) -> dict[str, MailMessageDetailVO]:
    """按需读取一封归属当前用户的邮件正文；不触发 LLM 或任何外部写入。"""
    try:
        detail = await mail_service.get_message_detail(session, current_user.user, email_id)
    except MailConnectionRequiredError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CONNECTION_REQUIRED", "message": str(error), "required_scopes": ["mail.read"]},
        ) from error
    except MailToolUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "MAIL_TOOL_UNAVAILABLE", "message": str(error)},
        ) from error
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件不存在或无权访问")
    return success(MailMessageDetailVO(
        email_id=email_id,
        thread_id=detail.provider_thread_id,
        from_=MailAddressVO(name=detail.sender_name, email=detail.sender_email),
        to=[MailAddressVO(email=value) for value in detail.recipients],
        cc=[],
        subject=detail.subject,
        received_at=detail.received_at,
        body=detail.body_text,
        attachments=[MailAttachmentVO(name=item.file_name, content_type=item.content_type, size_bytes=item.size_bytes) for item in detail.attachments],
        source_version=detail.source_version,
    ))


@router.get("/drafts")
async def list_drafts(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
    mail_service: MailService = Depends(get_mail_service_from_request),
) -> dict[str, list[MailDraftListItemVO]]:
    """列出当前用户可继续编辑、待发送确认或发送失败的站内草稿。"""
    drafts = await mail_service.list_drafts(session, current_user.user)
    return success([_to_draft_list_vo(item) for item in drafts])


@router.get("/drafts/{draft_id}")
async def get_draft(
    draft_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
    mail_service: MailService = Depends(get_mail_service_from_request),
) -> dict[str, MailDraftDetailVO]:
    """读取一封站内草稿的完整正文，仅允许草稿所属用户访问。"""
    draft = await mail_service.get_draft(session, current_user.user, draft_id)
    if draft is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件草稿不存在或无权访问")
    return success(_to_draft_detail_vo(draft))


@router.patch("/drafts/{draft_id}")
async def update_draft(
    draft_id: UUID,
    param: MailDraftUpdateParam,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
    mail_service: MailService = Depends(get_mail_service_from_request),
) -> dict[str, MailDraftDetailVO]:
    """保存站内草稿修改；该操作不会发送邮件。"""
    draft = await mail_service.update_draft(session, current_user.user, draft_id, param)
    if draft is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件草稿不存在或无权访问")
    if draft == "not_editable":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="当前草稿状态不允许编辑")
    return success(_to_draft_detail_vo(draft))


@router.post("/drafts/{draft_id}/send-confirmations", status_code=status.HTTP_201_CREATED)
async def request_send_confirmation(
    draft_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
    mail_service: MailService = Depends(get_mail_service_from_request),
    dispatcher: RunDispatchService = Depends(get_run_dispatcher_from_request),
) -> dict[str, MailSendConfirmationVO]:
    """创建邮件发送确认项；只有批准后 Worker 才会调用邮件发送工具。"""
    result = await mail_service.request_send_confirmation(session, current_user.user, draft_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件草稿不存在或无权访问")
    if result == "connection_required":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CONNECTION_REQUIRED", "message": "尚未连接具有邮件发送权限的工作邮箱", "required_scopes": ["mail.send"]},
        )
    if result == "not_sendable":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="当前草稿状态不允许提交发送确认")
    # 先让邮件发送确认图运行到 interrupt(...) 并保存 Checkpoint；审批决定才会恢复图。
    dispatcher.enqueue_after_commit(result.run_id)
    return success(MailSendConfirmationVO(
        run_id=result.run_id, approval_item_id=result.approval_item_id,
        approval_url=f"/approvals/{result.approval_item_id}",
        events_url=f"/runs/{result.run_id}/events",
    ))


@router.get("/sent-messages")
async def list_sent_messages(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
    mail_service: MailService = Depends(get_mail_service_from_request),
) -> dict[str, list[SentMailMessageVO]]:
    """列出当前用户已发送或发送失败的邮件记录。"""
    sent_messages = await mail_service.list_sent_messages(session, current_user.user)
    return success(
        [
            SentMailMessageVO(
                sent_message_id=item["id"],
                draft_id=item["draft_id"],
                provider_message_id=item["provider_message_id"],
                subject=item["subject"],
                recipients=item["recipient_summary"],
                sent_at=item["sent_at"],
                status=item["status"],
                run_id=item["run_id"],
            )
            for item in sent_messages
        ]
    )


@router.post("/{email_id}/todo-drafts", status_code=status.HTTP_201_CREATED)
async def create_todo_drafts(
    email_id: UUID,
    param: TodoDraftCreateParam,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
    mail_service: MailService = Depends(get_mail_service_from_request),
) -> dict[str, list[TodoDraftVO]]:
    """从已完成的摘要结果复制待办候选；不触发新的模型调用。"""
    drafts = await mail_service.create_todo_drafts(session, current_user.user, email_id, param)
    if drafts is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件、摘要结果或待办候选不存在")
    return success([TodoDraftVO(todo_draft_id=item["id"], email_id=item["email_id"], extraction_id=item["extraction_id"], content=item["content"], due_at=item["due_at"], status=item["status"], created_at=item["created_at"]) for item in drafts])


@router.post("/{email_id}/extraction-requests", status_code=status.HTTP_202_ACCEPTED)
async def request_extraction(
    email_id: UUID,
    param: MailExtractionRequestParam,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
    mail_service: MailService = Depends(get_mail_service_from_request),
    dispatcher: RunDispatchService = Depends(get_run_dispatcher_from_request),
) -> dict[str, MailAsyncRequestVO]:
    """提交或复用邮件摘要与待办提取任务。"""
    request_result = await mail_service.request_extraction(session, current_user.user, email_id, param)
    if request_result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件不存在或无权访问")
    if request_result.run_id is not None:
        dispatcher.enqueue_after_commit(request_result.run_id)
    return success(_to_async_request_vo(request_result))


@router.post("/{email_id}/reply-draft-requests", status_code=status.HTTP_202_ACCEPTED)
async def request_reply_draft(
    email_id: UUID,
    param: MailReplyDraftRequestParam,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
    mail_service: MailService = Depends(get_mail_service_from_request),
    dispatcher: RunDispatchService = Depends(get_run_dispatcher_from_request),
) -> dict[str, MailAsyncRequestVO]:
    """提交邮件回复草稿生成任务；任务完成前绝不发送邮件。"""
    request_result = await mail_service.request_reply_draft(session, current_user.user, email_id, param)
    if request_result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件或摘要结果不存在或无权访问")
    assert request_result.run_id is not None
    dispatcher.enqueue_after_commit(request_result.run_id)
    return success(_to_async_request_vo(request_result))


def _to_draft_list_vo(item: dict) -> MailDraftListItemVO:
    """将草稿仓储行转换为列表响应，列表中不携带正文。"""
    recipients = item["recipients"]
    return MailDraftListItemVO(
        draft_id=item["id"], source_email_id=item["source_email_id"], subject=item["subject"],
        to=recipients["to"], cc=recipients["cc"], version=item["version"],
        status=item["status"], updated_at=item["updated_at"],
    )


def _to_draft_detail_vo(item: dict) -> MailDraftDetailVO:
    """将草稿仓储行转换为详情响应。"""
    return MailDraftDetailVO(**_to_draft_list_vo(item).model_dump(), body=item["body"])


def _to_async_request_vo(request_result) -> MailAsyncRequestVO:
    """将后台任务受理结果转换为前端可直接订阅的 SSE 地址。"""
    events_url = f"/runs/{request_result.run_id}/events" if request_result.run_id else None
    return MailAsyncRequestVO(
        status=request_result.status,
        run_id=request_result.run_id,
        extraction_id=request_result.extraction_id,
        cached=request_result.cached,
        events_url=events_url,
        extraction=request_result.extraction,
    )
