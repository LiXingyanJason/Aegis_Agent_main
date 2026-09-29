import http from './http'

export async function listConversations() {
  const response = await http.get('/conversations')
  return response.data.data
}

export async function createConversation(title) {
  const response = await http.post('/conversations', title ? { title } : {})
  return response.data.data
}

export async function getConversationDetail(conversationId) {
  const response = await http.get(`/conversations/${conversationId}`)
  return response.data.data
}

export async function sendConversationMessage(conversationId, content, clientMessageId) {
  const response = await http.post(
    `/conversations/${conversationId}/messages`,
    { content, client_message_id: clientMessageId },
    { headers: { 'Idempotency-Key': clientMessageId } },
  )
  return response.data.data
}
