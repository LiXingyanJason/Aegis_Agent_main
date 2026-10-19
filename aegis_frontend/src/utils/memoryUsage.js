export function memoryUsageState(value) {
  return { available: '当前仍有效', changed: '版本已变化', inactive: '当前未生效或来源失效', restricted: '当前受限', deleted: '已删除', unknown: '历史状态未知' }[value] || '未知'
}

export function memoryPurposeLabel(value) {
  return { conversation_reply: '最终回复', mail_reply_draft: '邮件起草', mail_draft_revision: '草稿完善',
    calendar_planning: '日历规划', calendar_draft_personalization: '会议草稿', calendar_recommendation: '时间推荐',
    main_routing: '主路由', main_routing_repair: '主路由修复', mail_extraction: '邮件事实提取',
    mail_planning: '邮件规划', mail_collection: '邮件信息收集', knowledge_planning: '知识检索规划',
    knowledge_answer: '知识回答' }[value] || '其他模型调用'
}

export function tokenCount(value) {
  return Number.isSafeInteger(value) && value >= 0 ? String(value) : '未知'
}

export function memoryAttemptStatus(value) {
  return { succeeded: '已完成', failed: '失败', running: '进行中', pending: '待执行',
    timeout: '已超时', cancelled: '已取消' }[value] || '状态未知'
}

export function memorySelectionReason(value) {
  return { selected: '已选择相关记忆', selected_limited: '已选择部分记忆，受额度限制',
    selected_with_exclusions: '已选择部分记忆，存在保守排除',
    no_relevant_memory: '没有符合条件的相关记忆', memory_conflict_excluded: '冲突记忆未注入',
    memory_budget_exhausted: '记忆输入额度不足', purpose_denied: '本次用途不允许使用记忆',
    memory_service_unavailable: '记忆服务不可用', memory_audit_failed: '记忆使用审计未完成',
    relevant_confirmed_preference: '相关且已确认的设置', confirmed_summary: '已确认的长期摘要',
    session_fact: '当前会话事实' }[value] || '未记录选择原因'
}

export function memorySourceKind(value) {
  return { manual: '手动添加', user_input: '用户输入', user_confirmed: '用户确认',
    run: '任务', knowledge_document: '知识资料' }[value] || '来源未知'
}

export function memoryExclusionLabels(counts = {}) {
  const labels = { entity_preferred: '已优先使用结构化设置', summary_conflict: '摘要冲突，未注入',
    ambiguous_summary: '含义不明确，未注入', duplicate_memory: '重复记忆', not_relevant: '不相关',
    purpose_type_denied: '用途不允许此类型', entity_key_denied: '用途不允许此设置', invalid_record: '无效记录',
    item_budget: '条数额度不足', char_budget: '字符额度不足', byte_budget: '字节额度不足',
    input_budget: '本次输入额度不足', recall_limit: '达到候选召回上限' }
  return Object.entries(labels).filter(([key]) => Number.isInteger(counts[key]) && counts[key] > 0)
    .map(([key, label]) => ({ key, label, count: counts[key] }))
}
