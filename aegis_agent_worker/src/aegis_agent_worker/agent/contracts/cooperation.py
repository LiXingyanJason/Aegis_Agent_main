"""8.1固定交接契约；不允许模型指定任意工作流、工具或身份。"""
import hashlib
import json
import re
from typing import Literal, Annotated
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator
from aegis_agent_worker.agent.contracts.decision import ResourceReference
from aegis_agent_worker.agent.contracts.calendar import CalendarDecision


class TaskStepInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    version: Literal[1] = 1
    step_id: Literal['mail_requirements', 'calendar_candidates', 'meeting_draft']
    source: ResourceReference | None = None

    @model_validator(mode='after')
    def email_source(self):
        if self.step_id!='mail_requirements' and self.source is None:
            raise ValueError('后续步骤必须携带已核验邮件来源')
        if self.source and (self.source.resource_type != 'email' or not self.source.source_version or self.source.version is not None):
            raise ValueError('交接只接受带来源版本的邮件引用')
        return self


class TaskStepResult(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    version: Literal[1] = 1
    step_id: Literal['mail_requirements', 'calendar_candidates', 'meeting_draft']
    status: Literal['succeeded', 'unavailable', 'waiting_confirmation', 'completed', 'rejected']
    source: ResourceReference | None = None
    source_digest: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    calendar_decision: CalendarDecision | None = None
    candidate_ids: tuple[Annotated[str,Field(min_length=8,max_length=64,pattern=r'^slot_[a-f0-9]+$')], ...] = Field(default=(), max_length=20)
    candidate_version: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    draft_id: UUID | None = None
    approval_item_id: UUID | None = None
    issue_code: Literal['mail_not_found', 'mail_ambiguous', 'source_changed', 'requirements_unclear',
        'tool_unavailable', 'calendar_unavailable', 'draft_unavailable'] | None = None

    @model_validator(mode='after')
    def result_shape(self):
        if self.source is not None:
            TaskStepInput(step_id=self.step_id,source=self.source)
        if self.status == 'unavailable':
            if not self.issue_code or self.calendar_decision or self.candidate_ids or self.draft_id or self.approval_item_id:
                raise ValueError('不可用结果不能携带可执行结果')
        elif self.issue_code:
            raise ValueError('成功结果不能含失败原因')
        if self.calendar_decision and self.calendar_decision.action != 'prepare_meeting':
            raise ValueError('协同只生成会议草稿')
        if self.status=='succeeded':
            if self.step_id=='mail_requirements':
                if self.source is None or self.source_digest is None or self.calendar_decision is None:
                    raise ValueError('邮件步骤缺少可信来源与计划')
            elif self.step_id=='calendar_candidates':
                if self.source is None or not self.candidate_ids or self.candidate_version is None:
                    raise ValueError('候选步骤缺少快照引用')
            else: raise ValueError('会议草稿必须明确等待确认或终态')
        if self.calendar_decision is not None and self.step_id!='mail_requirements':
            raise ValueError('其他步骤不能覆盖邮件计划')
        if self.status in {'waiting_confirmation','completed','rejected'} and (
                self.step_id!='meeting_draft' or self.source is None or self.draft_id is None or self.approval_item_id is None):
            raise ValueError('草稿结果必须携带原审批引用')
        if len(set(self.candidate_ids))!=len(self.candidate_ids):
            raise ValueError('候选引用不能重复')
        return self


class MailMeetingEvidence(BaseModel):
    """仅节点局部使用，不写入State、审计或Artifact。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    excerpt: str = Field(min_length=1, max_length=1000)


def snapshot_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def verify_grounded_calendar(decision, excerpt, user_request):
    """首版只接受可核验的绝对日期、钟点和分钟数；不猜邮件相对日期/用户承诺。"""
    if decision.action != 'prepare_meeting' or decision.time_mode != 'search_window':
        raise ValueError('需要明确的会议搜索窗口')
    # 第一版不静默合并用户另提的日期/钟点/相对时间。明确改期应澄清或走原单领域安排。
    if re.search(r'\d{4}[-年]\d|\d{1,2}:\d{2}|今天|明天|后天|下周|本周|周[一二三四五六日天]|上午|下午|晚上|小时',user_request):
        raise ValueError('用户额外时间要求需要单独核对')
    requested_duration=re.search(r'(\d{1,3})\s*分钟',user_request)
    if ('分钟' in user_request and requested_duration is None) or (requested_duration and int(requested_duration[1])!=decision.duration_minutes):
        raise ValueError('用户与邮件时长不一致或无法核验')
    # 首版限定北京时间绝对窗口，拒绝偏移/多窗口/相对时间而不是宽松拼接不同片段。
    if decision.timezone != 'Asia/Shanghai' or re.search(r'UTC|GMT|[+-]\d{2}:\d{2}|\dZ\b',excerpt,re.I):
        raise ValueError('首版需要明确的北京时间窗口')
    pattern=r'(?P<date>\d{4}-\d{2}-\d{2}|\d{4}年\d{1,2}月\d{1,2}日)|(?P<time>(?<!\d)\d{1,2}:\d{2}(?!\d))'
    current_date=None;instants=[]
    for match in re.finditer(pattern,excerpt):
        if match.group('date'):
            year,month,day=map(int,re.findall(r'\d+',match.group()))
            current_date=f'{year:04d}-{month:02d}-{day:02d}'
        else:
            if current_date is None: raise ValueError('钟点缺少日期')
            hour,minute=map(int,match.group().split(':'))
            instants.append((current_date,f'{hour:02d}:{minute:02d}'))
    expected=[(instant.strftime('%Y-%m-%d'),instant.strftime('%H:%M')) for instant in (decision.start_at,decision.end_at)]
    if any(instant.second or instant.microsecond for instant in (decision.start_at,decision.end_at)):
        raise ValueError('邮件只明确到分钟')
    if instants!=expected:
        raise ValueError('日期/钟点必须与单个原文窗口逐一一致')
    if not re.search(rf'(?<!\d){decision.duration_minutes}\s*(?:分钟|minutes?\b)', excerpt, re.I):
        raise ValueError('时长缺少邮件依据')
    # 邮件第三方地址不是用户明确选择的参会人；首版只接收本轮显式地址。
    if any(address not in user_request.lower() for address in decision.attendees):
        raise ValueError('参会人未经用户明确选择')
    # 带偏移的原文由CalendarAgent处理；代码仍限制可用IANA时区与原文钟点。
    return decision
