"""日历候选与会议确认的业务复核及安全呈现。"""
from datetime import datetime
from zoneinfo import ZoneInfo
from aegis_agent_worker.agent.contracts.cooperation import TaskStepResult,snapshot_digest
from aegis_agent_worker.agent.routing.cooperation_registry import CooperationPlanError
from ...runtime.run_context import run_from
from ...runtime.result_handler import StepResultHandler


async def verify_calendar(service,state,result):
    source=TaskStepResult.model_validate(state['cooperation_results']['mail_requirements'])
    if result.source!=source.source: raise CooperationPlanError('step_source_mismatch')
    await service.verify_source(run_from(state),source.source,source.source_digest)
    if result.candidate_version!=snapshot_digest(state['availability']) or tuple(result.candidate_ids)!=tuple(
            item['slot_id'] for item in state['availability']['candidates']):
        raise CooperationPlanError('candidate_snapshot_changed')


async def verify_draft(service,state,result):
    source=TaskStepResult.model_validate(state['cooperation_results']['mail_requirements'])
    if result.source!=source.source: raise CooperationPlanError('step_source_mismatch')
    await service.verify_source(run_from(state),source.source,source.source_digest)
    if str(result.draft_id)!=state.get('draft_id') or str(result.approval_item_id)!=state.get('approval_item_id'):
        raise CooperationPlanError('draft_reference_changed')
    await service.verify_draft(run_from(state),result)


def present_candidates(state):
    decision=TaskStepResult.model_validate(state['cooperation_results']['mail_requirements']).calendar_decision
    timezone=ZoneInfo(decision.timezone)
    rows=[]
    for item in state['availability']['candidates']:
        start=datetime.fromisoformat(item['start_at']).astimezone(timezone)
        end=datetime.fromisoformat(item['end_at']).astimezone(timezone)
        rows.append(f"{start:%Y-%m-%d %H:%M} ～ {end:%Y-%m-%d %H:%M}")
    return ('已按邮件时间要求查询当前用户的空闲候选（'+decision.timezone+'）：\n'+
            '\n'.join(rows)+'\n这些是可用窗口，并非已安排会议；未创建草稿或写入日历，也不代表其他参会人空闲。')


_FAILURE_UPDATES=(('confirmation_error','协同步骤不可用，已停止会议草稿处理。'),)
_FAILURE_CONTENT='协同步骤未完成，已停止后续执行；不会自动重试或重新写入日历。'

CALENDAR_RESULT_HANDLERS={
    'CalendarCandidatesV1':StepResultHandler(verify_calendar,'calendar_unavailable',present_candidates,
        failure_updates=_FAILURE_UPDATES,failure_content=_FAILURE_CONTENT),
    'MeetingDraftV1':StepResultHandler(verify_draft,'draft_unavailable',
        failure_updates=_FAILURE_UPDATES,failure_content=_FAILURE_CONTENT),
}
