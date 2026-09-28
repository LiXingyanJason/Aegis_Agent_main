"""会话业务逻辑。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.param.conversation_create_param import ConversationCreateParam
from app.repository.conversation_repository import Conversation, ConversationRepository
from app.repository.user_repository import LocalUser


class ConversationService:
    """处理会话创建等面向用户的业务规则。"""

    def __init__(self, conversations: ConversationRepository | None = None) -> None:
        self._conversations = conversations or ConversationRepository()

    async def create_conversation(
        self,
        session: AsyncSession,
        local_user: LocalUser,
        param: ConversationCreateParam,
    ) -> Conversation:
        """使用已认证用户的本地租户和用户标识创建会话。"""
        return await self._conversations.create(
            session,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
            title=param.title or "新对话",
        )
