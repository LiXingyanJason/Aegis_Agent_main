import http from './http'

export async function createConversation(title) {
  const response = await http.post('/conversations', title ? { title } : {})
  return response.data.data
}

export async function sendConversationMessage(conversationId, content) {
  const response = await http.post(
    `/conversations/${conversationId}/messages`,
    { content, client_message_id: crypto.randomUUID() },
    { headers: { 'Idempotency-Key': crypto.randomUUID() } },
  )
  return response.data.data
}
