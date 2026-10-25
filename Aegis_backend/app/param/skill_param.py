"""User-authored guides are data, never executable code or authority."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

Purpose = Literal['mail_reply_draft','mail_draft_revision']

class SkillContent(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1,max_length=64,pattern=r'^[a-z][a-z0-9-]*$')
    description: str = Field(min_length=1,max_length=300)
    body: str = Field(min_length=1,max_length=6000)
    purposes: list[Purpose] = Field(min_length=1,max_length=2)

    @field_validator('purposes')
    @classmethod
    def unique(cls,value):
        if len(set(value)) != len(value): raise ValueError('用途不能重复')
        return sorted(value)

class SkillUpdate(SkillContent):
    expected_revision: int = Field(ge=1)

class SkillRevision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)

class SkillEnabled(SkillRevision):
    enabled: bool
