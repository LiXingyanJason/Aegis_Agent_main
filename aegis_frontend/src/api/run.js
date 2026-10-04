import http from './http'

export async function getRunDetail(runId) {
  const response = await http.get('/runs/' + runId)
  return response.data.data
}
