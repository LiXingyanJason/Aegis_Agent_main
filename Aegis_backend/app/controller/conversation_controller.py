"""会话相关 HTTP 接口。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.response import success
from app.param.conversation_create_param import ConversationCreateParam
from app.param.message_send_param import MessageSendParam
from app.security.dependencies import CurrentUser, get_current_user, get_tenant_session
from app.service.conversation_service import ConversationService
from app.service.run_dispatch_service import RunDispatchService, get_run_dispatcher_from_request
from app.vo.conversation_vo import (
    ApprovalItemVO,
    ConversationDetailVO,
    ConversationListItemVO,
    ConversationMessageVO,
    ConversationRunVO,
    ConversationVO,
    MessageSendVO,
    RunStepVO,
    ToolPreviewVO,
)

# 此文件中的接口路径都会自动加上 /conversations 前缀。
# 在接口文档中，将这些接口归类到 conversations 分组。
router = APIRouter(prefix="/conversations", tags=["conversations"])
_conversation_service = ConversationService()


# current_user 由 FastAPI 通过 Depends(get_current_user) 自动注入。
# get_current_user依赖 HTTPBearer框架会从请求Header的Authorization:Bearer<access_token>中解析JWT完成身份验证与本地用户映射。
@router.post("", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    param: ConversationCreateParam,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session), # 返回带RLS规则的数据库事务会话
) -> dict[str, ConversationVO]:
    """创建会话；仅接受已认证用户，并在其租户 RLS 事务中写入会话数据。"""
    conversation = await _conversation_service.create_conversation(session, current_user.user, param)
    response = ConversationVO(
        conversation_id=conversation.id,
        title=conversation.title,
        status=conversation.status,
        created_at=conversation.created_at,
    )
    return success(response)


@router.post("/{conversation_id}/messages", status_code=status.HTTP_202_ACCEPTED)
async def send_message(
    conversation_id: UUID,
    param: MessageSendParam,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
    dispatcher: RunDispatchService = Depends(get_run_dispatcher_from_request), # 需要用到运行期资源：Redis 和配置
) -> dict[str, MessageSendVO]:
    """保存用户任务消息，并创建一个等待 Agent 消费的 queued 任务运行。"""
    sent_message_run = await _conversation_service.send_message(
        session, current_user.user, conversation_id, param
    ) # 在 PostgreSQL 事务中写入，但还没有提交数据库事务
    if sent_message_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在或无权访问")
    if sent_message_run.status == "queued":
        dispatcher.enqueue_after_commit(sent_message_run.run_id) # 先创建该入redis队列申请，等待提交数据库事务后，再真正入队

    response = MessageSendVO(
        message_id=sent_message_run.message_id,
        run_id=sent_message_run.run_id,
        status=sent_message_run.status,
        idempotent_replay=sent_message_run.is_replay,
    )
    return success(response)


@router.get("")
async def list_conversations(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, list[ConversationListItemVO]]:
    """返回当前登录用户的历史会话摘要，供前端选择并恢复会话。"""
    conversations = await _conversation_service.list_conversations(session, current_user.user)
    response = [
        ConversationListItemVO(
            conversation_id=item["id"],
            title=item["title"],
            status=item["status"],
            last_message_at=item["last_message_at"],
            latest_message_preview=item["latest_message_preview"],
            created_at=item["created_at"],
        )
        for item in conversations
    ]
    return success(response)


@router.get("/{conversation_id}")
async def get_conversation_detail(
    conversation_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, ConversationDetailVO]:
    """恢复指定会话的可见消息及其执行进度、工具预览、待确认操作。"""
    detail = await _conversation_service.get_conversation_detail(
        session, current_user.user, conversation_id
    )
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在或无权访问")

    conversation = detail["conversation"]
    response = ConversationDetailVO(
        conversation_id=conversation_id,
        title=conversation["title"],
        status=conversation["status"],
        last_message_at=conversation["last_message_at"],
        created_at=conversation["created_at"],
        updated_at=conversation["updated_at"],
        messages=[
            ConversationMessageVO(
                message_id=message["id"],
                role=message["role"],
                content=message["display_content"],
                sequence_no=message["sequence_no"],
                is_final=message["is_final"],
                run_id=message["run_id"],
                tool_call_id=message["tool_call_id"],
                created_at=message["created_at"],
            )
            for message in detail["messages"]
        ],
        runs=[
            ConversationRunVO(
                run_id=run["id"],
                status=run["status"],
                current_stage=run["current_stage"],
                result_summary=run["result_summary"],
                error_code=run["error_code"],
                error_message=run["error_message"],
                started_at=run["started_at"],
                finished_at=run["finished_at"],
                created_at=run["created_at"],
                steps=[
                    RunStepVO(
                        step_id=step["id"],
                        sequence_no=step["sequence_no"],
                        step_key=step["step_key"],
                        label=step["label"],
                        status=step["status"],
                        detail=step["detail"],
                        started_at=step["started_at"],
                        finished_at=step["finished_at"],
                    )
                    for step in run["steps"]
                ],
                tool_previews=[
                    ToolPreviewVO(
                        tool_call_id=tool["id"],
                        tool_name=tool["tool_name"],
                        risk_level=tool["risk_level"],
                        input_summary=tool["input_summary"],
                        output_summary=tool["output_summary"],
                        status=tool["status"],
                        error_code=tool["error_code"],
                        error_message=tool["error_message"],
                        duration_ms=tool["duration_ms"],
                        created_at=tool["created_at"],
                    )
                    for tool in run["tool_previews"]
                ],
                approval_items=[
                    ApprovalItemVO(
                        approval_item_id=approval["id"],
                        action=approval["action"],
                        risk_level=approval["risk_level"],
                        resource_type=approval["resource_type"],
                        resource_id=approval["resource_id"],
                        title=approval["title"],
                        preview_snapshot=approval["preview_snapshot"],
                        draft_version=approval["draft_version"],
                        status=approval["status"],
                        expires_at=approval["expires_at"],
                        created_at=approval["created_at"],
                    )
                    for approval in run["approval_items"]
                ],
            )
            for run in detail["runs"]
        ],
    )
    return success(response)
