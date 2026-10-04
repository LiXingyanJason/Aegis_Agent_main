import http from './http'
import { getAccessToken } from '../utils/oidc'

export async function getRunDetail(runId) {
  const response = await http.get('/runs/' + runId)
  return response.data.data
}

function runEventsUrl(runId) {
  const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL || '/api/v1').replace(/\/$/, '')
  return apiBaseUrl + '/runs/' + encodeURIComponent(runId) + '/events'
}

function createHttpError(response) {
  const error = new Error('实时任务连接失败（HTTP ' + response.status + '）。')
  error.status = response.status
  return error
}

function parseSseEvent(frame) {
  const fields = { data: [] }
  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue
    const separator = line.indexOf(':')
    const field = separator === -1 ? line : line.slice(0, separator)
    const value = separator === -1 ? '' : line.slice(separator + 1).replace(/^ /, '')
    if (field === 'data') fields.data.push(value)
    else if (field === 'id' || field === 'event') fields[field] = value
  }

  if (!fields.data.length) return null
  return {
    id: fields.id,
    event: fields.event || 'message',
    data: JSON.parse(fields.data.join('\n')),
  }
}

/**
 * 使用 fetch 订阅受 Bearer Token 保护的 SSE 流。
 * 浏览器的 EventSource 不能添加 Authorization 请求头，因此不能用于该接口。
 */
export async function subscribeRunEvents(
  runId,
  { lastEventId, signal, onOpen, onEvent } = {},
) {
  const token = await getAccessToken()
  if (!token) throw new Error('登录已失效，请重新登录。')

  const headers = {
    Accept: 'text/event-stream',
    Authorization: 'Bearer ' + token,
  }
  if (lastEventId !== undefined && lastEventId !== null) {
    headers['Last-Event-ID'] = String(lastEventId)
  }

  const response = await fetch(runEventsUrl(runId), {
    method: 'GET',
    headers,
    signal,
  })
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('aegis:unauthorized'))
    throw createHttpError(response)
  }
  if (!response.body) throw new Error('实时任务连接未返回可读取的数据流。')

  await onOpen?.()

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r\n/g, '\n')

      let delimiterIndex = buffer.indexOf('\n\n')
      while (delimiterIndex !== -1) {
        const frame = buffer.slice(0, delimiterIndex)
        buffer = buffer.slice(delimiterIndex + 2)
        const event = parseSseEvent(frame)
        if (event) await onEvent?.(event)
        delimiterIndex = buffer.indexOf('\n\n')
      }
      if (done) break
    }
  } finally {
    reader.releaseLock()
  }
}
