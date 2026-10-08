"""邮件 Provider 的最小接口。"""

from typing import Protocol

from aegis_email_mcp.schemas.email import (
    EmailMessage,
    EmailRequestContext,
    EmailSendParam,
    SentEmailVO,
)


class EmailProvider(Protocol):
    """屏蔽 Mock、Gmail、Outlook 等邮件平台差异。"""

    async def list_messages(self, context: EmailRequestContext) -> list[EmailMessage]:
        """读取当前连接下的全部邮件列表快照。"""

    async def get_message(
        self, context: EmailRequestContext, provider_message_id: str
    ) -> EmailMessage | None:
        """按邮件平台标识读取单封邮件的正文和附件元数据。"""

    async def send_message(
        self, context: EmailRequestContext, param: EmailSendParam
    ) -> SentEmailVO:
        """发送一封已通过主后端审批的邮件。"""
