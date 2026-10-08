"""邮件工具的输入、输出和领域模型。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


@dataclass(frozen=True, slots=True)
class EmailRequestContext:
    """主后端在 MCP `_meta` 中传入的无状态调用上下文。"""

    connection_id: UUID
    tenant_id: UUID
    user_id: UUID
    # 邮件列表同步由 API 直接发起时没有 agent_run；摘要或起草任务发起时才携带 run_id。
    run_id: UUID | None


def parse_request_context(meta: dict[str, Any] | None) -> EmailRequestContext:
    """仅校验调用上下文格式；身份、租户和权限由主后端负责。"""
    if not isinstance(meta, dict):
        raise ValueError("缺少有效的 Aegis 调用上下文")
    try:
        return EmailRequestContext(
            connection_id=UUID(str(meta["aegis_connection_id"])),
            tenant_id=UUID(str(meta["aegis_tenant_id"])),
            user_id=UUID(str(meta["aegis_user_id"])),
            run_id=(
                UUID(str(meta["aegis_run_id"]))
                if meta.get("aegis_run_id") is not None
                else None
            ),
        )
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("缺少有效的 Aegis 连接与身份上下文") from error


class AttachmentVO(BaseModel):
    """邮件附件的安全元数据，不含附件内容。"""

    file_name: str
    content_type: str
    size_bytes: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """Provider 返回的一封标准化邮件。"""

    provider_message_id: str
    provider_thread_id: str | None
    sender_name: str | None
    sender_email: str
    recipients: tuple[str, ...]
    subject: str
    received_at: datetime
    source_version: str
    list_preview: str
    body_text: str
    attachments: tuple[AttachmentVO, ...]


class EmailMessageListItemVO(BaseModel):
    """列表场景的邮件快照；故意不返回正文。"""

    provider_message_id: str
    provider_thread_id: str | None
    sender_name: str | None
    sender_email: str
    subject: str
    received_at: datetime
    source_version: str
    list_preview: str
    attachment_count: int

    @classmethod
    def from_entity(cls, message: EmailMessage) -> "EmailMessageListItemVO":
        return cls(
            provider_message_id=message.provider_message_id,
            provider_thread_id=message.provider_thread_id,
            sender_name=message.sender_name,
            sender_email=message.sender_email,
            subject=message.subject,
            received_at=message.received_at,
            source_version=message.source_version,
            list_preview=message.list_preview,
            attachment_count=len(message.attachments),
        )


class EmailMessagesVO(BaseModel):
    """mail.messages.list 的只读结果。"""

    source: str = "mock_mailbox"
    messages: list[EmailMessageListItemVO]


class EmailMessageDetailVO(EmailMessageListItemVO):
    """mail.messages.get 的邮件全文结果。"""

    recipients: list[str]
    body_text: str
    attachments: list[AttachmentVO]

    @classmethod
    def from_entity(cls, message: EmailMessage) -> "EmailMessageDetailVO":
        return cls(
            **EmailMessageListItemVO.from_entity(message).model_dump(),
            recipients=list(message.recipients),
            body_text=message.body_text,
            attachments=list(message.attachments),
        )


class EmailSendParam(BaseModel):
    """已获 Aegis 批准后才可提交给邮件 Provider 的发送参数。"""

    to: list[str] = Field(min_length=1)
    cc: list[str] = Field(default_factory=list)
    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=65535)
    idempotency_key: str = Field(min_length=1, max_length=128)


class SentEmailVO(BaseModel):
    """邮件 Provider 确认发送后的最小回执。"""

    provider_message_id: str
    sent_at: datetime
    status: str = "sent"
    recipients: dict[str, list[str]]
