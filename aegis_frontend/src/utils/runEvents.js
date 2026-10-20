const AUDIT_ONLY_RUN_EVENTS = new Set([
  'cooperation_step_completed',
  'cooperation_plan_completed',
  'cooperation_schedule_step_completed',
  'cooperation_completed',
])

export function isAuditOnlyRunEvent(eventType) {
  return AUDIT_ONLY_RUN_EVENTS.has(eventType)
}
