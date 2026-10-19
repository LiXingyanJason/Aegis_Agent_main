import http from './http'

export async function clearSessionMemory() {
  // 使用当前Bearer身份，不传tenant/user；退出不能被Redis故障阻塞。
  await http.delete('/session-memory', { timeout: 2000 })
}
