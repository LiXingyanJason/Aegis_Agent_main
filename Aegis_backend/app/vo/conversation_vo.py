"""会话接口的响应模型。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class ConversationVO(BaseModel):
    """新建或读取会话时返回给页面的基础信息。"""

    conversation_id: UUID
    title: str
    status: str
    created_at: datetime


class ConversationListItemVO(BaseModel):
    """历史会话列表中的单个摘要。"""

    conversation_id: UUID
    title: str
    status: str
    last_message_at: datetime | None
    latest_message_preview: str | None
    created_at: datetime


class ConversationMessageVO(BaseModel):
    """恢复会话时展示的一条消息。"""

    message_id: UUID
    role: str
    content: str | None
    sequence_no: int
    is_final: bool
    run_id: UUID | None
    tool_call_id: UUID | None
    created_at: datetime


class RunStepVO(BaseModel):
    """任务运行的单个进度步骤。"""

    step_id: UUID
    sequence_no: int
    step_key: str
    label: str
    status: str
    detail: str | None
    started_at: datetime | None
    finished_at: datetime | None


class ToolPreviewVO(BaseModel):
    """对话页面可展示的工具调用概要。"""

    tool_call_id: UUID
    tool_name: str
    risk_level: str
    input_summary: str | None
    output_summary: str | None
    status: str
    error_code: str | None
    error_message: str | None
    duration_ms: int | None
    created_at: datetime


class ApprovalItemVO(BaseModel):
    """需要用户逐项确认的外部操作。"""

    approval_item_id: UUID
    action: str
    risk_level: str
    resource_type: str | None
    resource_id: str | None
    title: str
    preview_snapshot: dict[str, Any] | None
    draft_version: int | None
    status: str
    expires_at: datetime | None
    created_at: datetime


class ConversationRunVO(BaseModel):
    """会话内一次 Agent 运行及其可视化执行信息。"""

    run_id: UUID
    status: str
    current_stage: str | None
    result_summary: str | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    steps: list[RunStepVO]
    tool_previews: list[ToolPreviewVO]
    approval_items: list[ApprovalItemVO]


class ConversationDetailVO(BaseModel):
    """恢复一个完整会话时返回的页面数据。"""

    conversation_id: UUID
    title: str
    status: str
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime
    messages: list[ConversationMessageVO]
    runs: list[ConversationRunVO]


class MessageSendVO(BaseModel):
    """发送任务消息后返回的标识及初始运行状态。"""

    message_id: UUID
    run_id: UUID
    status: str
    idempotent_replay: bool
