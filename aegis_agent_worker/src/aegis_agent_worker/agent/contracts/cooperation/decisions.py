"""协调者只决定下一次委派，不输出全量DAG、工具或权限。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CooperationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["delegate", "finish", "finish_unavailable"]
    expert: str | None = Field(default=None, min_length=1, max_length=64)
    goal: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "delegate":
            if not self.expert or not self.goal or not self.goal.strip():
                raise ValueError("委派必须包含专家及非空子目标")
        elif self.expert is not None or self.goal is not None:
            raise ValueError("结束决定不能携带委派参数")
        return self
