import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'

let server
let api
const calls = []

before(async () => {
  globalThis.__skillHttp = {
    get: async (...args) => { calls.push(['get', ...args]); return { data: { data: { items: [] } } } },
    post: async (...args) => { calls.push(['post', ...args]); return { data: { data: { revision: 2 } } } },
    put: async (...args) => { calls.push(['put', ...args]); return { data: { data: { revision: 3 } } } },
    patch: async (...args) => { calls.push(['patch', ...args]); return { data: { data: { revision: 4 } } } },
    delete: async (...args) => { calls.push(['delete', ...args]); return { data: { data: { revision: 5 } } } },
  }
  server = await createServer({ plugins: [{
    name: 'skills-test-http', enforce: 'pre',
    resolveId(source, importer) {
      if (source === './http' && importer?.replaceAll('\\', '/').endsWith('/api/skills.js')) return '\0skills-mock-http'
    },
    load(id) {
      if (id === '\0skills-mock-http') return 'export default globalThis.__skillHttp'
    },
  }], server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  api = await server.ssrLoadModule('/src/api/skills.js')
})
after(async () => { await server?.close(); delete globalThis.__skillHttp })

test('Skill 管理接口遵循发布、启停和软删除的 revision 契约', async () => {
  calls.length = 0
  const content = { name: 'reply-guide', description: '邮件回复指南', body: '# 指南', purposes: ['mail_reply_draft'] }
  await api.listSkillTemplates()
  await api.listSkills()
  await api.createSkill(content)
  await api.getSkill('skill-id')
  await api.updateSkill('skill-id', { ...content, expected_revision: 2 })
  await api.publishSkill('skill-id', 3)
  await api.setSkillEnabled('skill-id', 4, false)
  await api.deleteSkill('skill-id', 5)
  assert.deepEqual(calls, [
    ['get', '/skills/templates'],
    ['get', '/skills', { params: { limit: 100 } }],
    ['post', '/skills', content],
    ['get', '/skills/skill-id'],
    ['put', '/skills/skill-id', { ...content, expected_revision: 2 }],
    ['post', '/skills/skill-id/publish', { expected_revision: 3 }],
    ['patch', '/skills/skill-id/enabled', { expected_revision: 4, enabled: false }],
    ['delete', '/skills/skill-id', { data: { expected_revision: 5 } }],
  ])
})

test('Skill 历史版本和使用记录按需读取，游标不混用', async () => {
  calls.length = 0
  await api.listSkillVersions('skill-id', 7)
  await api.getSkillVersion('skill-id', 6)
  await api.getRunSkillUsages('run-id', 'attempt-id')
  assert.deepEqual(calls, [
    ['get', '/skills/skill-id/versions', { params: { limit: 20, before: 7 } }],
    ['get', '/skills/skill-id/versions/6'],
    ['get', '/skills/runs/run-id/usages', { params: { limit: 50, cursor: 'attempt-id' } }],
  ])
})
