"""协同展示快照；只暴露安全字段，旧运行缺失时为null。"""
import json
from datetime import datetime
from typing import Literal
from pydantic import BaseModel,Field


class CooperationProgressVO(BaseModel):
    schema_version: Literal[1]
    execution_mode: Literal['fixed','scheduled']
    stage: Literal['started','planning','step_started','step_finished','waiting_confirmation','blocked','finished','failed','cancelled']
    status: Literal['running','succeeded','waiting_confirmation','completed','rejected','unavailable','blocked','failed','cancelled']
    label: str=Field(max_length=100)
    phase: Literal['mail_requirements','calendar_candidates','meeting_draft']|None=None
    domain: Literal['mail','calendar']|None=None
    step_id: str|None=Field(default=None,max_length=32,pattern=r'^[a-z][a-z0-9_]*$')
    event_no: int=Field(ge=1)
    occurred_at: datetime|None=None


def cooperation_snapshot(value):
    if value is None: return None
    try:
        if isinstance(value,str): value=json.loads(value)
        return CooperationProgressVO.model_validate(value)
    except (ValueError,TypeError):
        # 展示投影损坏/版本不支持时不影响权威任务状态或授权查询。
        return None
