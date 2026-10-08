"""从邮件提取结果复制待办草稿时的请求参数。"""

from uuid import UUID

from pydantic import BaseModel, Field


class TodoDraftCreateParam(BaseModel):
    """只允许选择已经由摘要流程生成的待办候选索引。"""

    extraction_id: UUID
    todo_indexes: list[int] = Field(min_length=1, max_length=50)
