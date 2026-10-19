import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createPinia, setActivePinia } from 'pinia'

let server, useAuthStore
before(async () => {
  server = await createServer({ plugins: [{
    name: 'session-memory-auth-mocks', enforce: 'pre',
    resolveId(source, importer) {
      if (!importer?.replaceAll('\\', '/').endsWith('/store/auth.js')) return
      if (source === '../utils/oidc') return '\0session-oidc'
      if (source === '../api/sessionMemory') return '\0session-api'
    },
    load(id) {
      if (id === '\0session-oidc') return `export const keycloak = { authenticated: true };
        export const getProfile = () => ({}); export const initializeOidc = async () => true;
        export const login = async () => {}; export const logout = async () => globalThis.__sessionLogout();`
      if (id === '\0session-api') return `export const clearSessionMemory = async () => globalThis.__sessionClear();`
    },
  }], server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  useAuthStore = (await server.ssrLoadModule('/src/store/auth.js')).useAuthStore
})
after(async () => { await server?.close() })

test('sign out clears work cache before OIDC logout', async () => {
  setActivePinia(createPinia())
  const calls = []
  globalThis.__sessionClear = async () => calls.push('clear')
  globalThis.__sessionLogout = async () => calls.push('logout')
  const auth = useAuthStore()
  auth.authenticated = true
  await auth.signOut()
  assert.deepEqual(calls, ['clear', 'logout'])
  assert.equal(auth.authenticated, false)
})

test('cache failure does not prevent logout', async () => {
  setActivePinia(createPinia())
  let loggedOut = false
  globalThis.__sessionClear = async () => { throw new Error('offline') }
  globalThis.__sessionLogout = async () => { loggedOut = true }
  await useAuthStore().signOut()
  assert.equal(loggedOut, true)
})
