"""待办计划页面的返回模型。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class TodoVO(BaseModel):
    """一项由邮件摘要派生、可在 Aegis 内部维护的待办。"""

    todo_id: UUID
    email_id: UUID
    extraction_id: UUID
    content: str
    due_at: datetime | None
    status: str
    completed_at: datetime | None
    source_subject: str
    source_sender: str
    created_at: datetime
    updated_at: datetime
