export function inputRequestType(item) {
  if (item?.input_type == null || item.input_type === 'text') return 'text'
  if (item.input_type === 'single_choice') return 'single_choice'
  return 'unsupported'
}

export function buildInputAnswerPayload(item, { response, selectedOptionId }, clientResponseId) {
  const base = {
    expected_version: item.version,
    client_response_id: clientResponseId,
  }
  const type = inputRequestType(item)
  if (type === 'single_choice') {
    if (!selectedOptionId || !Array.isArray(item.options)
      || !item.options.some((option) => option.id === selectedOptionId)) return null
    return { ...base, selected_option_id: selectedOptionId }
  }
  if (type === 'text') {
    const content = response?.trim()
    return content ? { ...base, response: content } : null
  }
  return null
}

export function inputAnswerKey(item, payload) {
  return `${item.question_id}:${item.version}:${inputRequestType(item)}:${payload.selected_option_id ?? payload.response}`
}

export function inputRequestBelongsToScope(item, { scope, conversationId, runIds }, runConversationIds) {
  if (scope === 'conversation') {
    return Boolean(conversationId) && (runIds?.includes(item.run_id)
      || runConversationIds.get(item.run_id) === conversationId)
  }
  if (scope === 'mail') return runConversationIds.has(item.run_id)
    && runConversationIds.get(item.run_id) === null
  return scope === 'all' && (!runIds || runIds.includes(item.run_id))
}

export function pendingInputRunsAfterMessage(messages, runs, index) {
  const runId = messages[index]?.run_id
  if (!runId || messages.findLastIndex((message) => message.run_id === runId) !== index) return []
  return runs.filter((run) => run.run_id === runId && run.status === 'waiting_input')
}

export function unanchoredPendingInputRuns(messages, runs) {
  const messageRunIds = new Set(messages.map((message) => message.run_id).filter(Boolean))
  return runs.filter((run) => run.status === 'waiting_input' && !messageRunIds.has(run.run_id))
}
