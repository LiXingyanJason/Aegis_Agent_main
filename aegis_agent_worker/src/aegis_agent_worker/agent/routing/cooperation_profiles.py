"""可信任务的目标、效果上限与能力范围；不声明执行步骤或顺序。"""
from dataclasses import dataclass


EFFECT_LEVELS={'read':0,'internal_draft':1,'approved_external_write':2}


@dataclass(frozen=True)
class CooperationGoal:
    output_contract: str
    required_evidence: tuple[str,...]
    missing_information: str='clarify'

    def __post_init__(self):
        if (not self.output_contract or self.output_contract in self.required_evidence
                or len(set(self.required_evidence))!=len(self.required_evidence)
                or self.missing_information!='clarify'):
            raise ValueError('无效协同目标或未支持的缺信息策略')

    def describe(self):
        return dict(output_contract=self.output_contract,required_evidence=list(self.required_evidence),
                    missing_information=self.missing_information)


@dataclass(frozen=True)
class CooperationRiskLimit:
    max_effect: str='read'
    external_write: str='forbidden'
    approval_grant: bool=False

    def __post_init__(self):
        if (self.max_effect not in EFFECT_LEVELS or self.external_write not in {'forbidden','approval_only'}
                or self.approval_grant is not False
                or (self.external_write=='approval_only' and self.max_effect!='approved_external_write')):
            raise ValueError('风险上限不能授予审批或隐式开放外部写入')

    def permits(self,effect):
        return (effect in EFFECT_LEVELS and EFFECT_LEVELS[effect]<=EFFECT_LEVELS[self.max_effect]
                and (effect!='approved_external_write' or self.external_write=='approval_only'))

    def describe(self):
        return dict(max_effect=self.max_effect,external_write=self.external_write,approval_grant=False)


@dataclass(frozen=True)
class CooperationProfile:
    name: str
    domain: str
    intent: str
    available_capabilities: frozenset[str]
    goal: CooperationGoal
    description: str
    risk_limit: CooperationRiskLimit=CooperationRiskLimit()

    def __post_init__(self):
        if (not self.available_capabilities or not isinstance(self.goal,CooperationGoal)
                or not isinstance(self.risk_limit,CooperationRiskLimit)):
            raise ValueError('任务必须声明目标、风险上限和可用能力')

    def describe(self):
        return dict(name=self.name,domain=self.domain,intent=self.intent,goal=self.goal.describe(),
                    risk_limit=self.risk_limit.describe(),description=self.description,
                    available_capabilities=sorted(self.available_capabilities))


MAIL_CALENDAR_PROFILES=(
    CooperationProfile('mail-calendar-meeting-v4','mail','arrange_meeting_from_mail',
        frozenset({'mail.mail_requirements','calendar.calendar_candidates','calendar.meeting_draft'}),
        CooperationGoal('MeetingDraftV1',('MailRequirementsV1','CalendarCandidatesV1')),
        '依据明确邮件的时间要求查询可信候选、形成会议草稿，只有用户批准的版本可以写入日历。',
        CooperationRiskLimit('approved_external_write','approval_only')),
    CooperationProfile('mail-calendar-availability-v2','mail','find_free_time_from_mail',
        frozenset({'mail.mail_requirements','calendar.calendar_candidates'}),
        CooperationGoal('CalendarCandidatesV1',('MailRequirementsV1',)),
        '仅按邮件时间要求查询当前用户可信空闲候选，不生成草稿、不申请审批、不写日历。'),
)
