import http from './http'

export async function listKnowledgeDocuments(limit = 100) {
  const response = await http.get('/knowledge/documents', { params: { limit } })
  return response.data.data
}

export async function importKnowledgeText(payload) {
  const response = await http.post('/knowledge/documents', payload)
  return response.data.data
}

export async function uploadKnowledgeFile(file, metadata) {
  const form = new FormData()
  form.append('file', file)
  form.append('metadata', JSON.stringify(metadata))
  const response = await http.post('/knowledge/documents/upload', form, { timeout: 60_000 })
  return response.data.data
}

export async function startKnowledgeIndex(documentId) {
  const response = await http.post(`/knowledge/documents/${documentId}/index-jobs`, {
    external_embedding_allowed: true,
  })
  return response.data.data
}

export async function getKnowledgeIndexJob(jobId) {
  const response = await http.get(`/knowledge/index-jobs/${jobId}`)
  return response.data.data
}

export async function retryKnowledgeIndex(jobId) {
  const response = await http.post(`/knowledge/index-jobs/${jobId}/retry`)
  return response.data.data
}

export async function setKnowledgeActivation(documentId, enabled) {
  const response = await http.patch(`/knowledge/documents/${documentId}/activation`, { enabled })
  return response.data.data
}
