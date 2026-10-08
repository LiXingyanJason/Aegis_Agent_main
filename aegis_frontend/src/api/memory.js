import http from './http'

export async function listMemories({ q, cursor, limit = 50 } = {}) {
  const response = await http.get('/memories', {
    params: {
      ...(q ? { q } : {}),
      ...(cursor ? { cursor } : {}),
      limit,
    },
  })
  return response.data.data
}

export async function createMemory(payload) {
  const response = await http.post('/memories', payload)
  return response.data.data
}

export async function confirmMemory(payload) {
  const response = await http.post('/memories/confirm', payload)
  return response.data.data
}

export async function getMemory(memoryId) {
  const response = await http.get('/memories/' + memoryId)
  return response.data.data
}

export async function updateMemory(memoryId, payload) {
  const response = await http.patch('/memories/' + memoryId, payload)
  return response.data.data
}

export async function requestMemoryDeletion(memoryId) {
  const response = await http.post('/memories/' + memoryId + '/deletion-requests')
  return response.data.data
}

export async function deleteMemory(memoryId, deletionToken) {
  const response = await http.post('/memories/' + memoryId + '/delete', {
    deletion_token: deletionToken,
  })
  return response.data.data
}
