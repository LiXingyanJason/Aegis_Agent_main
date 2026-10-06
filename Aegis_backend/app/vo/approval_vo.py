"""逐项确认接口的响应模型。"""

from uuid import UUID
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ApprovalDecisionVO(BaseModel):
    """已被记录的批准或拒绝结果。"""

    approval_item_id: UUID
    run_id: UUID
    decision: str
    approval_status: str
    execution_scheduled: bool


class ApprovalItemVO(BaseModel):
    """确认页列表及详情共用的权威确认项。"""

    approval_item_id: UUID
    run_id: UUID
    action: str
    risk_level: str
    resource_type: str
    resource_id: UUID
    title: str
    preview_snapshot: dict[str, Any]
    draft_version: int | None
    status: str
    expires_at: datetime
    created_at: datetime
