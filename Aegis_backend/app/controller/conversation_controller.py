"""会话相关 HTTP 接口。"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.response import success
from app.param.conversation_create_param import ConversationCreateParam
from app.security.dependencies import CurrentUser, get_current_user, get_tenant_session
from app.service.conversation_service import ConversationService
from app.vo.conversation_vo import ConversationVO

router = APIRouter(prefix="/conversations", tags=["conversations"])
_conversation_service = ConversationService()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    param: ConversationCreateParam,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, ConversationVO]:
    """创建会话；仅接受已认证用户，并在其租户 RLS 事务中写入数据。"""
    conversation = await _conversation_service.create_conversation(session, current_user.user, param)
    response = ConversationVO(
        conversation_id=conversation.id,
        title=conversation.title,
        status=conversation.status,
        created_at=conversation.created_at,
    )
    return success(response)
