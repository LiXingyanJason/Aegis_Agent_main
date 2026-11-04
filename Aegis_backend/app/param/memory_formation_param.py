"""User-owned formation settings; no model may supply identity or consent."""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field,StrictBool,model_validator


class MemoryFormationParam(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version: int = Field(strict=True,ge=0)
    mode: Literal['off','suggest','controlled_auto'] = 'suggest'
    use_existing: StrictBool = True
    external_processing_allowed: StrictBool = False
    auto_approval_consent: StrictBool = False
    allowed_categories: list[Literal['preference','stable_fact','project_context','temporary_fact']] = Field(
        default_factory=lambda:['preference'],max_length=4)
    excluded_conversation_ids: list[UUID] = Field(default_factory=list,max_length=100)

    @model_validator(mode='after')
    def consistent(self):
        if len(set(self.allowed_categories))!=len(self.allowed_categories) or len(set(self.excluded_conversation_ids))!=len(self.excluded_conversation_ids):
            raise ValueError('Repeated setting')
        if self.mode=='controlled_auto' and not self.auto_approval_consent:
            raise ValueError('Controlled auto requires explicit consent')
        if self.mode!='controlled_auto' and self.auto_approval_consent:
            raise ValueError('Auto consent requires controlled auto mode')
        return self
