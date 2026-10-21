"""团队共享的是有界结果数据，不是专家全部局部上下文。"""
from pydantic import BaseModel, ConfigDict, Field

from .messages import ExpertResult


class TeamContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: str = Field(min_length=1, max_length=2000)
    results: tuple[ExpertResult, ...] = Field(default=(), max_length=32)
