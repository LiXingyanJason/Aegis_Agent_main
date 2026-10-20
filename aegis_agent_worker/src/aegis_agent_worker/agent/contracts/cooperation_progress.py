"""仅供展示的协同阶段；标签由代码生成，不接受Prompt或业务正文。"""
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator

PHASE_LABELS={
    'mail_requirements':'邮件助手正在提取会议要求',
    'calendar_candidates':'日历助手正在查询空闲时间',
    'meeting_draft':'正在生成会议草稿',
}


class CooperationProgress(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    schema_version: Literal[1]=1
    execution_mode: Literal['scheduled']
    stage: Literal['started','planning','step_started','step_finished','waiting_confirmation','blocked','finished','failed','cancelled']
    status: Literal['running','succeeded','waiting_confirmation','completed','rejected','unavailable','blocked','failed','cancelled']
    phase: Literal['mail_requirements','calendar_candidates','meeting_draft']|None=None
    step_id: str|None=Field(default=None,max_length=32,pattern=r'^[a-z][a-z0-9_]*$')

    @model_validator(mode='after')
    def valid_stage(self):
        allowed={'started':{'running'},'planning':{'running'},'step_started':{'running'},
            'step_finished':{'succeeded','completed','rejected','unavailable'},
            'waiting_confirmation':{'waiting_confirmation'},'blocked':{'blocked'},
            'finished':{'completed','rejected','unavailable'},'failed':{'failed'},'cancelled':{'cancelled'}}
        if self.status not in allowed[self.stage]: raise ValueError('协同阶段与状态不匹配')
        if self.stage in {'step_started','step_finished','blocked','waiting_confirmation'} and self.phase is None:
            raise ValueError('步骤阶段必须提供已注册领域阶段')
        if self.stage=='waiting_confirmation' and self.phase!='meeting_draft':
            raise ValueError('只有会议草稿能等待审批')
        return self

    def payload(self):
        label={'started':'正在协同处理邮件与日历','planning':'正在制定协同计划',
            'waiting_confirmation':'会议草稿待确认','blocked':'后续步骤因依赖失败已停止',
            'failed':'协同处理失败','cancelled':'协同处理已取消',
            'finished':{'completed':'协同处理已结束','rejected':'已拒绝创建会议','unavailable':'协同处理未全部完成'}.get(self.status)}.get(self.stage)
        if self.stage=='step_started': label=PHASE_LABELS[self.phase]
        if self.stage=='step_finished':
            name={'mail_requirements':'邮件会议要求','calendar_candidates':'日历候选','meeting_draft':'会议操作结果'}[self.phase]
            label=name+({'succeeded':'已核验','completed':'已核验','rejected':'已拒绝','unavailable':'不可用'}[self.status])
        return {**self.model_dump(mode='json'),'domain':'mail' if self.phase=='mail_requirements' else 'calendar' if self.phase else None,'label':label}
