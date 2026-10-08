"""任务运行查询接口的响应模型。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class RunStepDetailVO(BaseModel):
    """任务页面轮询时展示的一条执行步骤。"""

    step_id: UUID
    sequence_no: int
    step_key: str
    label: str
    status: str
    detail: str | None
    started_at: datetime | None
    finished_at: datetime | None


class RunDetailVO(BaseModel):
    """一次 Agent 任务运行的当前状态和可展示进度。"""

    run_id: UUID
    conversation_id: UUID | None
    status: str
    current_stage: str | None
    model_provider: str | None
    model_name: str | None
    result_summary: str | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    steps: list[RunStepDetailVO]
