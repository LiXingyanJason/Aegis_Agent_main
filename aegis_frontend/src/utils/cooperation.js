export const cooperationPhases = {
  started: '开始协作',
  coordinating: '协调下一步',
  delegated: '已委派专家',
  expert_started: '专家正在处理',
  expert_completed: '专家已返回',
  waiting_input: '等待用户补充',
  resumed: '继续处理原任务',
  draft_ready: '站内会议草稿已就绪',
  finished: '团队生成阶段结束',
  awaiting_confirmation: '等待用户核对会议',
  unavailable: '协作暂无法完成',
  failed: '协作执行受阻',
}

export function cooperationActorLabel(actor) {
  return { coordinator: '协调者', mail: '邮件专家', calendar: '日历专家', approval: '确认执行' }[actor] ?? '未关联处理者'
}

export function cooperationResultLabel(status) {
  return { completed: '已返回核验结果', unavailable: '暂不可用', failed: '执行受阻' }[status] ?? status
}

export function inputKindTitle(kind) {
  return {
    mail_selection: '请选择用于安排会议的邮件',
    mail_requirements: '请补充邮件中的会议要求',
    calendar_requirements: '请补充或核对会议安排要求',
  }[kind] ?? '需要补充信息'
}

export function inputKindHint(kind, inputType = 'text') {
  if (kind === 'mail_selection' && inputType === 'single_choice') {
    return '请从本题候选中选择一封邮件；选择不会直接写入日历。'
  }
  return {
    mail_selection: '请按当前问题中的候选顺序回复序号，或填写完整主题。',
    mail_requirements: '请补充缺少的日期范围、时长等要求。',
    calendar_requirements: '请核对或补充完整方案；提交回答不代表批准写入日历。',
  }[kind] ?? ''
}

export function cooperationStageLabel(item) {
  const base = cooperationPhases[item.phase] ?? item.label ?? item.phase
  const actor = item.cooperation?.actor
  if (['delegated', 'expert_started', 'expert_completed', 'resumed'].includes(item.phase)
    && ['mail', 'calendar'].includes(actor)) {
    return `${cooperationActorLabel(actor)}：${base}`
  }
  return base
}

export function cooperationGroupKey(item) {
  const link = item?.cooperation
  if (!link?.team_id) return 'unlinked'
  return `${link.team_id}:${link.delegation_id ?? link.actor ?? 'coordinator'}`
}

export function groupToolPreviews(tools = []) {
  const groups = new Map()
  for (const tool of tools) {
    const key = cooperationGroupKey(tool)
    if (!groups.has(key)) groups.set(key, {
      key,
      label: key === 'unlinked' ? '未关联协作的工具调用'
        : `${cooperationActorLabel(tool.cooperation.actor)}${tool.cooperation.sequence ? ` · 委派 ${tool.cooperation.sequence}` : ''}`,
      tools: [],
    })
    groups.get(key).tools.push(tool)
  }
  return [...groups.values()]
}
