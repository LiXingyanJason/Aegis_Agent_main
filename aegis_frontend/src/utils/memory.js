// 长期记忆只从用户维护页面写入；不接入任务对话或自动提取。
const COMMON_TIMEZONES = Object.freeze([
  ['Asia/Shanghai', '北京 / 上海'], ['Asia/Hong_Kong', '香港'],
  ['Asia/Singapore', '新加坡'], ['Asia/Tokyo', '东京'], ['Asia/Seoul', '首尔'],
  ['Asia/Kolkata', '印度'], ['Asia/Dubai', '迪拜'], ['Australia/Sydney', '悉尼'],
  ['Europe/London', '伦敦'], ['Europe/Berlin', '柏林'], ['Europe/Paris', '巴黎'],
  ['America/New_York', '纽约'], ['America/Los_Angeles', '洛杉矶'],
  ['Pacific/Honolulu', '檀香山'], ['UTC', '协调世界时'],
].map(([value, city]) => Object.freeze({ value, label: `${city}（${value}）` })))

export function memoryTimezoneOptions(currentValue) {
  const options = [...COMMON_TIMEZONES]
  // 兼容旧记录中的其他合法IANA值，不因编辑表单而悄悄替换；仍由后端校验。
  if (currentValue && !options.some(option => option.value === currentValue)) {
    options.push({ value: currentValue, label: `${currentValue}（当前已保存时区）` })
  }
  return options
}

export function memoryFormFromRecord(memory) {
  return {
    content: memory.memory_type === 'entity' && memory.content_is_generated ? '' : memory.content, isSensitive: memory.is_sensitive,
    memoryType: memory.memory_type, entityKey: memory.entity?.key || 'timezone',
    entityValue: memory.entity?.value || 'Asia/Shanghai',
    contactName: memory.entity?.name || '', contactEmail: memory.entity?.email || '',
    meetingTimeOfDay: memory.entity?.preferred_time_of_day || 'any',
    meetingAvoidWeekdays: [...(memory.entity?.avoid_weekdays || [])],
    expiresAt: memory.expires_at ? new Date(memory.expires_at) : null,
    sourceKind: memory.source_kind, sourceObjectId: memory.source_object_id || '', sourceVersion: memory.source_version || '',
  }
}

export function memoryPayload(form, version) {
  const payload = {
    content: form.content.trim(), memory_type: form.memoryType, scope: 'user',
    sensitivity: form.isSensitive ? 'sensitive' : 'normal',
    expires_at: form.expiresAt ? new Date(form.expiresAt).toISOString() : null,
    source_kind: form.sourceKind,
  }
  if (form.memoryType === 'entity') {
    payload.entity = form.entityKey === 'meeting_preferences'
      ? { key: 'meeting_preferences', preferred_time_of_day: form.meetingTimeOfDay || 'any',
          avoid_weekdays: [...(form.meetingAvoidWeekdays || [])], strength: 'soft' }
      : form.entityKey === 'contact'
      ? { key: 'contact', name: form.contactName.trim(), email: form.contactEmail.trim() }
      : { key: form.entityKey, value: form.entityValue }
  }
  if (form.sourceKind !== 'manual') {
    payload.source_object_id = form.sourceObjectId.trim()
    if (form.sourceVersion.trim()) payload.source_version = form.sourceVersion.trim()
  }
  if (version !== undefined) payload.expected_version = version
  return payload
}

export function memoryConfirmPayload(memory, replacement) {
  const payload = { memory_id: memory.memory_id, expected_version: memory.version }
  if (replacement) {
    payload.replace_memory_id = replacement.memory_id
    payload.replace_version = replacement.version
  }
  return payload
}

export function memoryState(memory) {
  if (memory.status === 'superseded') return '已被替换'
  if (memory.status === 'pending') return '待确认'
  if (memory.status === 'revoked') return '已撤销'
  if (memory.source_available === false) return '来源失效'
  if (memory.expires_at && new Date(memory.expires_at).getTime() <= Date.now()) return '已过期'
  return memory.is_sensitive ? '已确认 · 不进入模型' : '已确认'
}

export function memoryTypeLabel(memory) {
  if (memory.memory_type !== 'entity') return '长期摘要'
  const entity = { timezone: '默认时区', reply_style: '邮件语气', contact: '常用联系人', meeting_preferences: '会议软偏好' }
  return `设置 · ${entity[memory.entity_key] ?? '其他'}`
}

export function memorySourceLabel(memory) {
  const source = { manual: '手动录入', run: '已完成任务', knowledge_document: '知识资料' }
  return source[memory.source_kind]
    ?? (memory.source === 'user_confirmed' ? '用户确认' : memory.source === 'user_input' ? '用户输入' : '来源未标明')
}

export function memoryStatusDisplay(memory) {
  const state = memoryState(memory)
  const label = state === '已确认 · 不进入模型' ? '已确认' : state
  const tone = label === '已确认' ? 'confirmed'
    : label === '待确认' ? 'pending'
      : ['来源失效', '已过期'].includes(label) ? 'unavailable' : 'inactive'
  return {
    label,
    tone,
    note: label === '已确认' && (memory.is_sensitive || memory.prompt_eligible === false)
      ? '不进入模型' : null,
  }
}

export function memoryErrorMessage(cause, fallback) {
  const detail = cause.response?.data?.detail
  // 不把Pydantic原始input（可能是被拒绝的秘密）复制到页面错误消息。
  if (Array.isArray(detail)) return '输入未通过校验，请核对类型、时区、邮箱、敏感级别、来源、有效期和版本。'
  if (detail && typeof detail === 'object') return detail.message || fallback
  return detail || cause.message || fallback
}
