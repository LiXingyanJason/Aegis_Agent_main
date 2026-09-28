"""会话接口的响应模型。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ConversationVO(BaseModel):
    """新建或读取会话时返回给页面的基础信息。"""

    conversation_id: UUID
    title: str
    status: str
    created_at: datetime
