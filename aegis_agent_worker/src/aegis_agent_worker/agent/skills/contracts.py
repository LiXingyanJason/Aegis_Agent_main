import hashlib
import json
from typing import Literal
from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field,model_validator

PURPOSES=frozenset({'mail_reply_draft','mail_draft_revision'})

class SkillContent(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    name:str=Field(min_length=1,max_length=64,pattern=r'^[a-z][a-z0-9-]*$')
    description:str=Field(min_length=1,max_length=300)
    body:str=Field(min_length=1,max_length=6000)
    purposes:list[Literal['mail_reply_draft','mail_draft_revision']]=Field(min_length=1,max_length=2)

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.purposes))!=len(self.purposes): raise ValueError('duplicate purpose')
        self.purposes=sorted(self.purposes)
        return self

    def digest(self):
        return hashlib.sha256(json.dumps(self.model_dump(),sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

class SkillSelection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    skill_id:UUID|None  # null explicitly means continue without a Skill

class SkillReadRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    skill_id:UUID
    purpose:Literal['mail_reply_draft','mail_draft_revision']
