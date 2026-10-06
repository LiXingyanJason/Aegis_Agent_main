import http from './http'

export async function listApprovalItems({ runId, status } = {}) {
  const response = await http.get('/approvals', {
    params: {
      ...(runId ? { run_id: runId } : {}),
      ...(status ? { status } : {}),
    },
  })
  return response.data.data
}

export async function getApprovalItem(approvalItemId) {
  const response = await http.get('/approvals/' + approvalItemId)
  return response.data.data
}

export async function submitApprovalDecision(approvalItemId, decision, reason) {
  const response = await http.post('/approvals/' + approvalItemId + '/decision', {
    decision,
    ...(reason ? { reason } : {}),
  })
  return response.data.data
}
