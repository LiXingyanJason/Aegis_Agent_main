"""会话业务逻辑。"""

from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID
from typing import Any

from app.param.conversation_create_param import ConversationCreateParam
from app.param.message_send_param import MessageSendParam
from app.repository.conversation_repository import Conversation, ConversationRepository, SentMessageRun
from app.repository.user_repository import LocalUser


class ConversationService:
    """处理会话创建等面向用户的业务规则。"""

    def __init__(self, conversations: ConversationRepository | None = None) -> None:
        self._conversations = conversations or ConversationRepository()

    async def create_conversation(
        self,
        session: AsyncSession, # 数据库事务会话
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

    async def list_conversations(
        self, session: AsyncSession, local_user: LocalUser
    ) -> list[dict[str, Any]]:
        """列出当前用户的历史会话摘要。"""
        return await self._conversations.list_for_user(
            session, tenant_id=local_user.tenant_id, user_id=local_user.id
        )

    async def get_conversation_detail(
        self, session: AsyncSession, local_user: LocalUser, conversation_id: UUID
    ) -> dict[str, Any] | None:
        """恢复一个会话的消息、任务进度、工具预览和逐项确认信息。"""
        conversation = await self._conversations.find_detail_for_user(
            session,
            conversation_id=conversation_id,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
        )
        if conversation is None:
            return None

        messages = await self._conversations.list_messages_for_conversation(
            session,
            conversation_id=conversation_id,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
        )
        runs = await self._conversations.list_runs_for_conversation(
            session,
            conversation_id=conversation_id,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
        )
        for run in runs:
            run["steps"] = await self._conversations.list_steps_for_run(
                session, run_id=run["id"], tenant_id=local_user.tenant_id
            )
            run["tool_previews"] = await self._conversations.list_tool_previews_for_run(
                session, run_id=run["id"], tenant_id=local_user.tenant_id
            )
            run["approval_items"] = await self._conversations.list_approval_items_for_run(
                session,
                run_id=run["id"],
                tenant_id=local_user.tenant_id,
                user_id=local_user.id,
            )
        return {"conversation": conversation, "messages": messages, "runs": runs}

    async def send_message(
        self,
        session: AsyncSession,
        local_user: LocalUser,
        conversation_id: UUID,
        param: MessageSendParam,
    ) -> SentMessageRun | None:
        """向本人会话写入消息并创建待执行的 Agent 任务。"""
        return await self._conversations.create_message_and_run(
            session,
            conversation_id=conversation_id,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
            content=param.content,
            client_message_id=param.client_message_id,
        )
