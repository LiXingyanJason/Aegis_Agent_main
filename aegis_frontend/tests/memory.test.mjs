import { test } from 'node:test'
import assert from 'node:assert/strict'
import { memoryPayload, memoryConfirmPayload, memoryState, memoryFormFromRecord, memoryErrorMessage, memoryTypeLabel, memorySourceLabel, memoryStatusDisplay } from '../src/utils/memory.js'

const form = {
  content: ' 默认时区 ', memoryType: 'entity', entityKey: 'timezone', entityValue: 'Asia/Shanghai',
  isSensitive: false, expiresAt: null, sourceKind: 'manual', sourceObjectId: '', sourceVersion: '',
}

test('manual settings payload cannot claim model provenance or confirmation', () => {
  const payload = memoryPayload(form)
  assert.deepEqual(payload.entity, { key: 'timezone', value: 'Asia/Shanghai' })
  assert.equal(payload.source_kind, 'manual')
  assert.equal(payload.content, '默认时区')
  for (const key of ['status', 'confirmed_at', 'tenant_id', 'user_id', 'source_run_id', 'source_object_id']) {
    assert.equal(key in payload, false)
  }
})

test('review uses the saved source version rather than unsaved form input', () => {
  const saved = { content: '规范化内容', is_sensitive: false, memory_type: 'long_term_summary',
    source_kind: 'run', source_object_id: 'real-run', source_version: 'server-verified-version' }
  const state = memoryFormFromRecord(saved)
  assert.equal(state.sourceVersion, 'server-verified-version')
  assert.equal(state.content, '规范化内容')
})

test('edits carry expected version; contact is structured and expires uses explicit offset', () => {
  const payload = memoryPayload({ ...form, entityKey: 'contact', contactName: ' 李莉 ', contactEmail: ' lili@example.com ', expiresAt: '2030-01-01T10:00:00+08:00' }, 3)
  assert.equal(payload.expected_version, 3)
  assert.deepEqual(payload.entity, { key: 'contact', name: '李莉', email: 'lili@example.com' })
  assert.equal(payload.expires_at, '2030-01-01T02:00:00.000Z')
})

test('confirm references saved version only; replacing requires an explicit old record', () => {
  const current = { memory_id: 'saved-id', version: 2, content: 'cannot override at confirm' }
  assert.deepEqual(memoryConfirmPayload(current), { memory_id: 'saved-id', expected_version: 2 })
  assert.deepEqual(memoryConfirmPayload(current, { memory_id: 'old-id', version: 4 }), {
    memory_id: 'saved-id', expected_version: 2, replace_memory_id: 'old-id', replace_version: 4,
  })
})

test('pending, source unavailable, expired, sensitive and superseded are visible states', () => {
  assert.equal(memoryState({ status: 'pending' }), '待确认')
  assert.equal(memoryState({ status: 'superseded' }), '已被替换')
  assert.equal(memoryState({ status: 'confirmed', source_available: false }), '来源失效')
  assert.equal(memoryState({ status: 'confirmed', expires_at: '2000-01-01T00:00:00Z' }), '已过期')
  assert.equal(memoryState({ status: 'confirmed', is_sensitive: true }), '已确认 · 不进入模型')
})

test('列表将类型、状态与来源分开展示，并保留敏感记忆限制', () => {
  assert.equal(memoryTypeLabel({ memory_type: 'entity', entity_key: 'reply_style' }), '设置 · 邮件语气')
  assert.equal(memoryTypeLabel({ memory_type: 'long_term_summary' }), '长期摘要')
  assert.equal(memorySourceLabel({ source_kind: 'knowledge_document' }), '知识资料')
  assert.deepEqual(memoryStatusDisplay({ status: 'confirmed', is_sensitive: true, prompt_eligible: false }), {
    label: '已确认', tone: 'confirmed', note: '不进入模型',
  })
  assert.equal(memoryStatusDisplay({ status: 'pending' }).tone, 'pending')
  assert.equal(memoryStatusDisplay({ status: 'confirmed', source_available: false }).label, '来源失效')
})

test('validation errors do not echo rejected secret inputs', () => {
  const message = memoryErrorMessage({ response: { data: { detail: [{ input: 'password: never-echo' }] } } }, '错误')
  assert.ok(message.includes('校验'))
  assert.ok(!message.includes('never-echo'))
})
