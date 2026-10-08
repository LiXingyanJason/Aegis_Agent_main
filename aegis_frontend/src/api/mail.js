import http from './http'

export async function listMailMessages() {
  const response = await http.get('/mail/messages')
  return response.data.data
}

export async function getMailMessageDetail(emailId) {
  const response = await http.get('/mail/messages/' + emailId)
  return response.data.data
}

export async function listMailDrafts() {
  const response = await http.get('/mail/drafts')
  return response.data.data
}

export async function getMailDraft(draftId) {
  const response = await http.get('/mail/drafts/' + draftId)
  return response.data.data
}

export async function updateMailDraft(draftId, payload) {
  const response = await http.patch('/mail/drafts/' + draftId, payload)
  return response.data.data
}

export async function listSentMailMessages() {
  const response = await http.get('/mail/sent-messages')
  return response.data.data
}

export async function createMailTodoDrafts(emailId, payload) {
  const response = await http.post('/mail/' + emailId + '/todo-drafts', payload)
  return response.data.data
}

export async function requestMailExtraction(emailId, { refresh = false } = {}) {
  const response = await http.post('/mail/' + emailId + '/extraction-requests', { refresh })
  return response.data.data
}

export async function requestMailReplyDraft(emailId, payload = {}) {
  const response = await http.post('/mail/' + emailId + '/reply-draft-requests', payload)
  return response.data.data
}

export async function requestMailSendConfirmation(draftId) {
  const response = await http.post('/mail/drafts/' + draftId + '/send-confirmations')
  return response.data.data
}
