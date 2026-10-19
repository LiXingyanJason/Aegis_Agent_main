"""用户取消确认，不能借此编辑内容或直接重新确认。"""
from pydantic import BaseModel,ConfigDict,Field


class MemoryRevokeParam(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version: int = Field(strict=True,ge=1)
