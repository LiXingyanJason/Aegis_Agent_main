"""将用户已确认内容写入长期记忆的请求参数。"""

from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field,model_validator

from app.param.memory_create_param import MemoryCreateParam


class MemoryConfirmParam(MemoryCreateParam):
    """兼容用户一次性明确确认新内容；纯手动录入无需伪造来源任务。"""

    replace_memory_id: UUID | None = None
    replace_version: int | None = Field(default=None,strict=True,ge=1)

    @model_validator(mode='after')
    def replacement_pair(self):
        if (self.replace_memory_id is None)!=(self.replace_version is None): raise ValueError('替换需提供旧记忆ID和版本')
        return self


class MemoryConfirmExistingParam(BaseModel):
    model_config=ConfigDict(extra='forbid')
    memory_id: UUID
    expected_version: int = Field(strict=True,ge=1)
    replace_memory_id: UUID | None = None
    replace_version: int | None = Field(default=None,strict=True,ge=1)

    @model_validator(mode='after')
    def replacement_pair(self):
        if (self.replace_memory_id is None)!=(self.replace_version is None): raise ValueError('替换需提供旧记忆ID和版本')
        return self
