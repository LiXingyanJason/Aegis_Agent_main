const stages = new Set(['started', 'planning', 'step_started', 'step_finished', 'waiting_confirmation', 'blocked', 'finished', 'failed', 'cancelled'])
const statuses = new Set(['running', 'succeeded', 'waiting_confirmation', 'completed', 'rejected', 'unavailable', 'blocked', 'failed', 'cancelled'])

export function normalizeCooperationProgress(value) {
  if (!value || value.schema_version !== 1 || !['fixed', 'scheduled'].includes(value.execution_mode)
    || !stages.has(value.stage) || !statuses.has(value.status) || typeof value.label !== 'string'
    || value.label.length > 100 || !Number.isSafeInteger(Number(value.event_no)) || Number(value.event_no) < 1) return null
  return {
    schema_version: 1, execution_mode: value.execution_mode, stage: value.stage, status: value.status,
    label: value.label, event_no: Number(value.event_no), occurred_at: value.occurred_at ?? null,
    phase: ['mail_requirements', 'calendar_candidates', 'meeting_draft'].includes(value.phase) ? value.phase : null,
    domain: ['mail', 'calendar'].includes(value.domain) ? value.domain : null,
    step_id: typeof value.step_id === 'string' && /^[a-z][a-z0-9_]{0,31}$/.test(value.step_id) ? value.step_id : null,
  }
}

export function latestCooperationProgress(previous, incoming) {
  const old = normalizeCooperationProgress(previous)
  const next = normalizeCooperationProgress(incoming)
  if (!next) return old
  return old && old.event_no > next.event_no ? old : next
}

export function cooperationDisplay(run) {
  const progress = normalizeCooperationProgress(run.cooperation)
  if (!progress) return null
  let status = progress.status
  let label = progress.label
  if (run.status === 'failed') { status = 'failed'; label = '协同处理失败' }
  else if (run.status === 'cancelled') { status = 'cancelled'; label = '协同处理已取消' }
  else if (run.status === 'waiting_confirmation') { status = 'waiting_confirmation'; label = '会议草稿待确认' }
  else if (run.status === 'completed' && ['running', 'succeeded', 'waiting_confirmation'].includes(status)) {
    status = 'completed'; label = '协同交互已结束，请查看执行结果'
  }
  return { ...progress, status, label, mode_label: progress.execution_mode === 'scheduled' ? '计划调度协同' : '固定协同',
    active: status === 'running' && ['queued', 'running'].includes(run.status) }
}
