"""邮件管理接口返回模型。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class MailMessageVO(BaseModel):
    """研究工作台所需的邮件元数据快照。"""

    email_id: UUID
    thread_id: str | None
    customer: str | None
    from_email: str
    subject: str
    received_at: datetime
    source_version: str
    extraction_status: str


class MailMessagesVO(BaseModel):
    """当前用户全部邮件列表及其本次同步时间。"""

    snapshot_at: datetime | None
    items: list[MailMessageVO]


class MailAttachmentVO(BaseModel):
    """邮件详情中的附件安全元数据；一期不提供下载。"""

    name: str
    content_type: str
    size_bytes: int


class MailAddressVO(BaseModel):
    """邮件详情中的可展示地址；Mock Provider 暂不提供联系人名称。"""

    name: str | None = None
    email: str


class MailMessageDetailVO(BaseModel):
    """用户按需打开的单封邮件详情。"""

    email_id: UUID
    thread_id: str | None
    from_: MailAddressVO = Field(serialization_alias="from")
    to: list[MailAddressVO]
    cc: list[MailAddressVO]
    subject: str
    received_at: datetime
    body: str
    body_format: str = "text"
    attachments: list[MailAttachmentVO]
    source_version: str


class MailDraftListItemVO(BaseModel):
    """保存草稿分区的列表项，不返回完整正文。"""

    draft_id: UUID
    source_email_id: UUID
    subject: str
    to: list[str]
    cc: list[str]
    version: int
    status: str
    updated_at: datetime


class MailDraftDetailVO(MailDraftListItemVO):
    """查看或编辑草稿时返回的完整站内草稿。"""

    body: str


class SentMailMessageVO(BaseModel):
    """已发送分区的邮件记录。"""

    sent_message_id: UUID
    draft_id: UUID
    provider_message_id: str
    subject: str
    recipients: dict
    sent_at: datetime
    status: str
    run_id: UUID | None


class TodoDraftVO(BaseModel):
    """从摘要候选复制到站内待办后的草稿。"""

    todo_draft_id: UUID
    email_id: UUID
    extraction_id: UUID
    content: str
    due_at: datetime | None
    status: str
    created_at: datetime


class MailAsyncRequestVO(BaseModel):
    """异步摘要或回复草稿任务的受理结果。"""

    status: str
    run_id: UUID | None = None
    extraction_id: UUID | None = None
    cached: bool = False
    events_url: str | None = None
    extraction: dict | None = None


class MailSendConfirmationVO(BaseModel):
    """提交邮件发送确认后的确认页导航信息。"""

    run_id: UUID
    approval_item_id: UUID
    status: str = "waiting_confirmation"
    approval_url: str
    events_url: str
