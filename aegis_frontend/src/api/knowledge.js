import http from './http'
import { knowledgeImportMetadata } from '../utils/knowledge'

export async function listKnowledgeDocuments(limit = 100, offset = 0) {
  const response = await http.get('/knowledge/documents', { params: { limit, offset } })
  return response.data.data
}

export async function importKnowledgeText(payload) {
  const response = await http.post('/knowledge/documents', {
    ...knowledgeImportMetadata(payload),
    content: payload.content,
  })
  return response.data.data
}

export async function uploadKnowledgeFile(file, metadata) {
  const form = new FormData()
  form.append('file', file)
  form.append('metadata', JSON.stringify(knowledgeImportMetadata(metadata)))
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

export async function getKnowledgeDocument(documentId) {
  return (await http.get(`/knowledge/documents/${encodeURIComponent(documentId)}`)).data.data
}

export async function deleteKnowledgeDocument(documentId) {
  await http.delete(`/knowledge/documents/${encodeURIComponent(documentId)}`)
}

export async function setKnowledgeVisibility(documentId, visibility) {
  return (await http.patch(`/knowledge/documents/${encodeURIComponent(documentId)}/visibility`, { visibility })).data.data
}

export async function getKnowledgeSource(source, signal) {
  const path = `/knowledge/documents/${encodeURIComponent(source.document_id)}/versions/${encodeURIComponent(source.version_id)}/chunks/${encodeURIComponent(source.chunk_id)}`
  return (await http.get(path, { params: { content_hash: source.content_hash }, signal })).data.data
}
