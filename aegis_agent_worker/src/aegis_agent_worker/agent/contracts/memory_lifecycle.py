"""Formation proposals are not authorized writes or active prompt memories."""
from hashlib import sha256
from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime,Field,model_validator
from .memory import MemoryModel,MemoryEntity


class MemoryEvidenceRef(MemoryModel):
    message_id: UUID
    content_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    start: int = Field(strict=True,ge=0)
    end: int = Field(strict=True,gt=0)
    @model_validator(mode='after')
    def span(self):
        if self.end<=self.start:raise ValueError('Invalid evidence range')
        return self
    def verify(self,message_id,text):
        if message_id!=self.message_id or sha256(text.encode()).hexdigest()!=self.content_hash or self.end>len(text):
            raise ValueError('Evidence snapshot changed')
        return text[self.start:self.end]


class MemoryExtractionMessage(MemoryModel):
    message_id: UUID
    role: Literal['user','assistant','tool']
    content: str = Field(min_length=1,max_length=8000)
    excluded: bool = False


class MemoryValueAssessment(MemoryModel):
    """Assessor proposal, not proof or authorization; no arbitrary importance score."""
    lifetime: Literal['transient','bounded','durable','uncertain']
    subject: Literal['self','third_party','hypothetical','uncertain']
    reusable: bool
    explicit_request: bool
    source_supported: bool
    sensitivity: Literal['normal','sensitive','secret']

    def disposition(self):
        if self.sensitivity!='normal':return 'reject_sensitive'
        if self.subject in ('third_party','hypothetical') or self.lifetime=='transient':return 'do_not_form'
        if not self.source_supported or self.subject=='uncertain' or self.lifetime=='uncertain':return 'needs_clarification'
        return 'candidate' if self.reusable or self.explicit_request else 'do_not_form'


class MemoryExtractionInput(MemoryModel):
    messages: tuple[MemoryExtractionMessage,...] = Field(min_length=1,max_length=20)
    @model_validator(mode='after')
    def bounded(self):
        if len({m.message_id for m in self.messages})!=len(self.messages) or sum(len(m.content) for m in self.messages)>16000:
            raise ValueError('Duplicate or excessive message window')
        return self


class MemoryProposal(MemoryModel):
    category: Literal['preference','stable_fact','project_context','temporary_fact']
    content: str = Field(min_length=1,max_length=4000)
    entity: MemoryEntity | None = None
    evidence: tuple[MemoryEvidenceRef,...] = Field(min_length=1,max_length=5)
    reason: Literal['explicit_request','reusable_preference','stable_user_fact','ongoing_project','time_bounded_fact']
    valid_from: AwareDatetime
    expires_at: AwareDatetime | None = None
    @model_validator(mode='after')
    def valid(self):
        from .memory import MemoryCandidate
        MemoryCandidate.safe_content(self.content)
        if self.expires_at and self.expires_at<=self.valid_from:raise ValueError('Invalid validity interval')
        if self.category=='temporary_fact' and self.expires_at is None:raise ValueError('Temporary fact requires expiry')
        if len({(e.message_id,e.start,e.end) for e in self.evidence})!=len(self.evidence):raise ValueError('Repeated evidence')
        return self
    def verify_evidence(self,window):
        messages={m.message_id:m for m in window.messages}
        for ref in self.evidence:
            message=messages.get(ref.message_id)
            if message is None or message.role!='user' or message.excluded:raise ValueError('Unsupported evidence source')
            ref.verify(message.message_id,message.content)
        return self


class MemoryChangePlan(MemoryModel):
    action: Literal['ADD','NOOP','PROPOSE_UPDATE','PROPOSE_SUPERSEDE','PROPOSE_RETRACT']
    target_id: UUID | None = None
    expected_version: int | None = Field(default=None,strict=True,ge=1)
    @model_validator(mode='after')
    def shape(self):
        if self.action.startswith('PROPOSE_') and (self.target_id is None or self.expected_version is None):
            raise ValueError('Mutation requires versioned target')
        if self.action in ('ADD','NOOP') and (self.target_id is not None or self.expected_version is not None):
            raise ValueError('Unexpected target')
        return self


class MemoryLifecycleEvent(MemoryModel):
    object_id: UUID
    object_version: int = Field(strict=True,ge=1)
    event_type: Literal['proposal_created','proposal_rejected','proposal_expired','proposal_applied','memory_invalidated','cleanup_requested','cleanup_completed','settings_changed']
    reason: str = Field(pattern=r'^[a-z][a-z0-9_]{0,99}$')


def transition_proposal(current,target):
    if current!='pending' or target not in ('rejected','expired','applied'):raise ValueError('Invalid proposal transition')
    return target
