"""邮件读取业务规则。"""

from aegis_email_mcp.providers.base import EmailProvider
from aegis_email_mcp.schemas.email import (
    EmailMessageDetailVO,
    EmailMessagesVO,
    EmailMessageListItemVO,
    EmailRequestContext,
)


class EmailService:
    """实现 Provider 无关的邮件读取规则。"""

    def __init__(self, provider: EmailProvider) -> None:
        self._provider = provider

    async def list_messages(self, context: EmailRequestContext) -> EmailMessagesVO:
        """返回当前用户邮箱的全部列表快照，不暴露正文。"""
        messages = await self._provider.list_messages(context)
        return EmailMessagesVO(
            messages=[EmailMessageListItemVO.from_entity(message) for message in messages]
        )

    async def get_message(
        self, context: EmailRequestContext, provider_message_id: str
    ) -> EmailMessageDetailVO:
        """返回一封邮件的正文和附件元数据。"""
        message = await self._provider.get_message(context, provider_message_id)
        if message is None:
            raise LookupError("邮件不存在或当前连接无权读取")
        return EmailMessageDetailVO.from_entity(message)
