import { cooperationStageLabel } from './cooperation.js'

const terminalStatuses = new Set(['completed', 'failed', 'cancelled'])
const activeStatuses = new Set(['running', 'in_progress', 'started'])

export function runProgressText(run, cooperation = []) {
  if (!run || terminalStatuses.has(run.status)) return ''
  if (run.status === 'queued') return '任务已提交，等待开始'
  if (run.status === 'waiting_input') return '等待你补充信息后继续'
  if (['waiting_confirmation', 'waiting_approval'].includes(run.status)) return '等待你核对并确认'

  const activeTool = [...(run.tool_previews ?? [])].reverse()
    .find((tool) => activeStatuses.has(tool.status) && tool.tool_name)
  if (activeTool) return `正在调用工具：${activeTool.tool_name}`

  const activeStep = [...(run.steps ?? [])].reverse()
    .find((step) => activeStatuses.has(step.status) && step.label)
  if (activeStep) {
    return /^(正在|等待)/.test(activeStep.label)
      ? activeStep.label : `正在处理：${activeStep.label}`
  }

  const latestCooperation = cooperation.at(-1)
  if (latestCooperation && !['finished', 'draft_ready'].includes(latestCooperation.phase)) {
    return cooperationStageLabel(latestCooperation)
  }

  const stage = run.current_stage?.trim()
  return stage && stage !== '等待处理' ? stage : '正在处理任务…'
}
