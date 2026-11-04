from datetime import datetime,timezone
from hashlib import sha256
from uuid import uuid4
import pytest
from aegis_agent_worker.agent.contracts.memory_lifecycle import MemoryEvidenceRef,MemoryExtractionInput,MemoryProposal,MemoryChangePlan,transition_proposal
from aegis_agent_worker.service.memory.formation_policy import formation_gate


def proposal():
    text='以后回复简洁';identifier=uuid4()
    ref=MemoryEvidenceRef(message_id=identifier,content_hash=sha256(text.encode()).hexdigest(),start=0,end=len(text))
    value=MemoryProposal(category='preference',content=text,evidence=(ref,),reason='explicit_request',valid_from=datetime.now(timezone.utc))
    window=MemoryExtractionInput(messages=({'message_id':identifier,'role':'user','content':text},))
    return value,window


def test_evidence_requires_exact_user_snapshot():
    value,window=proposal()
    assert value.verify_evidence(window)==value
    for changes in ({'role':'assistant'},{'excluded':True},{'content':'不同的消息'}):
        changed=window.model_copy(update={'messages':(window.messages[0].model_copy(update=changes),)})
        with pytest.raises(ValueError):value.verify_evidence(changed)


def test_state_and_mutation_contracts():
    assert transition_proposal('pending','applied')=='applied'
    for old,new in [('applied','pending'),('rejected','applied'),('pending','confirmed')]:
        with pytest.raises(ValueError):transition_proposal(old,new)
    with pytest.raises(ValueError):MemoryChangePlan(action='PROPOSE_UPDATE')
    with pytest.raises(ValueError):MemoryChangePlan(action='DELETE')
    value,_=proposal()
    with pytest.raises(ValueError):MemoryProposal(**{**value.model_dump(),'category':'temporary_fact'})


def test_value_assessment_cannot_promote_secrets_or_other_people():
    from aegis_agent_worker.agent.contracts.memory_lifecycle import MemoryValueAssessment
    base=dict(lifetime='durable',subject='self',reusable=True,explicit_request=True,source_supported=True,sensitivity='normal')
    assert MemoryValueAssessment(**base).disposition()=='candidate'
    for changes,expected in [({'sensitivity':'secret'},'reject_sensitive'),({'subject':'third_party'},'do_not_form'),
        ({'lifetime':'transient'},'do_not_form'),({'source_supported':False},'needs_clarification')]:
        assert MemoryValueAssessment(**(base|changes)).disposition()==expected


@pytest.mark.asyncio
async def test_usage_setting_is_checked_before_read_and_again_before_send():
    from unittest.mock import AsyncMock
    from test_memory_call_injection import setup
    from aegis_agent_worker.harness.exceptions import HarnessError
    run,service,_,_,repo,_=setup()
    repo.formation_use_existing=AsyncMock(return_value=False)
    bundle,reason=await service.prepare_call(run,purpose='conversation_reply',task_text='邮件语气',remaining_chars=10000)
    assert not bundle.items and reason=='memory_usage_disabled' and repo.reads==0
    with pytest.raises(HarnessError):await service.record_call(run,uuid4(),bundle)
    assert not repo.usages


@pytest.mark.parametrize('text,reason',[('以后回复简洁','eligible_for_extraction'),('这次回复简洁','temporary_or_quoted'),
    ('他说喜欢下午会议','temporary_or_quoted'),('不要记住这段','memory_control'),('密码是秘密','sensitive_or_secret'),('你好','no_information')])
def test_gate_before_extraction(text,reason):
    _,window=proposal();message=window.messages[0].model_copy(update={'content':text})
    assert formation_gate(message,mode='suggest',external_allowed=True)==reason
    assert formation_gate(message,mode='off',external_allowed=True)=='mode_off'
    assert formation_gate(message,mode='suggest',external_allowed=True,conversation_excluded=True)=='excluded_scope'
