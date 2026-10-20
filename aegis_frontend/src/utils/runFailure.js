const VERSION_ERROR_CODES = new Set([
  'POLICY_VERSION_UNAVAILABLE',
  'POLICY_VERSION_CONFLICT',
  'WORKFLOW_ROUTING_FAILED',
  'scheduler_version_mismatch',
  'plan_binding_changed',
  'cooperation_restart_blocked',
])

const VERSION_GUARD_REASONS = new Set([
  'scheduler_version_mismatch',
  'plan_binding_changed',
  'cooperation_restart_blocked',
])

export const VERSION_INCOMPATIBLE_MESSAGE = '任务版本不兼容，请联系管理员处理旧任务或重新发起。'

export function isVersionIncompatibleRun(run) {
  if (run?.status !== 'failed') return false
  if (VERSION_ERROR_CODES.has(run.error_code)) return true
  return VERSION_GUARD_REASONS.has(run.error_message)
}

export function runFailureReason(run) {
  if (isVersionIncompatibleRun(run)) return VERSION_INCOMPATIBLE_MESSAGE
  return {
    LLM_UNAVAILABLE: '模型服务调用失败，本次任务未能完成。',
    MODEL_OUTPUT_INVALID: '模型结果未通过校验，本次任务未能完成。',
    EXECUTION_BUDGET_EXCEEDED: '任务执行次数或时间预算已耗尽。',
    EXECUTION_TIMEOUT: '任务执行超时。',
    MEMORY_CONTEXT_CHANGED: '相关记忆或来源已变化，请重新核对。',
  }[run?.error_code] || '本次任务执行失败，未能完成请求。'
}
