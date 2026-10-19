import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createPinia, setActivePinia } from 'pinia'
import { citationParts, unavailableKnowledgeArtifacts, sameSourceSnapshot, knowledgeRetrievalSummary, knowledgeSearchError, knowledgeImportMetadata } from '../src/utils/knowledge.js'

let server
let Card
let FailureNotice
let useStore
before(async () => {
  server = await createServer({ plugins: [{
    name: 'knowledge-test-api', enforce: 'pre',
    resolveId(source, importer) {
      if (importer?.replaceAll('\\', '/').endsWith('/store/conversation.js') && ['../api/conversation', '../api/run'].includes(source)) return '\0knowledge-mock-api'
    },
    load(id) {
      if (id !== '\0knowledge-mock-api') return null
      return `export const getConversationArtifacts = (...args) => globalThis.__knowledgeArtifacts(...args);
        export const createConversation = () => {}; export const getConversationDetail = (...args) => globalThis.__knowledgeConversationDetail(...args);
        export const listConversations = () => {}; export const sendConversationMessage = () => {};
        export const getRunDetail = (...args) => globalThis.__knowledgeRunDetail(...args);`
    },
  }], server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  Card = (await server.ssrLoadModule('/src/components/KnowledgeSourceCard.vue')).default
  FailureNotice = (await server.ssrLoadModule('/src/components/RunFailureNotice.vue')).default
  useStore = (await server.ssrLoadModule('/src/store/conversation.js')).useConversationStore
})
after(async () => { await server?.close() })

const source = { source_no: 2, chunk_id: 'test', title: '<script>alert(1)</script>', version_label: 'v1',
  section: '政策', start_offset: 0, end_offset: 10, excerpt: '<img src=x onerror=alert(1)>', content_hash: 'a'.repeat(64) }

test('规范编号只链接到当前来源，未知或非规范编号保留纯文本', () => {
  const parts = citationParts('政策[1]，另一份[2]；[02] <script>x</script>', [source])
  assert.equal(parts.filter((part) => part.source).length, 1)
  assert.equal(parts.find((part) => part.source).text, '[2]')
  assert.equal(parts.map((part) => part.text).join(''), '政策[1]，另一份[2]；[02] <script>x</script>')
})

test('资料导入元数据不再包含类别字段', () => {
  const metadata = knowledgeImportMetadata({ title: '示例', client_request_id: 'new-request',
    visibility: 'private', external_embedding_allowed: false, is_sensitive: false, category: 'policy' })
  assert.deepEqual(metadata, { title: '示例', client_request_id: 'new-request', visibility: 'private',
    external_embedding_allowed: false, is_sensitive: false })
})

test('Hybrid 检索只展示证据数量和降级原因，不将 RRF 诊断分数当成可信度', () => {
  const summary = knowledgeRetrievalSummary({ evidence_count: 1, retrieval: {
    mode: 'lexical_only', degraded: true, reason: 'vector_unavailable',
    rankings: [{ chunk_id: 'test', lexical_rank: 1, vector_rank: null, rrf_score: 0.016 }],
  } })
  assert.deepEqual(summary, {
    count: 1, mode: '仅关键词检索', degraded: true,
    reason: '向量检索暂不可用，本次仅使用关键词检索。',
  })
  assert.ok(!JSON.stringify(summary).includes('0.016'))
  assert.equal(knowledgeRetrievalSummary({ evidence_count: 0, retrieval: { reason: 'no_accessible_documents' } }).reason,
    '当前没有可检索的已授权资料。')
  assert.match(knowledgeSearchError({ error_code: 'KNOWLEDGE_SCOPE_TOO_LARGE' }), /缩小范围/)
})

test('刷新失败只清除知识来源缓存，不改变其他卡片或原对象', () => {
  const artifacts = [{ artifact_type: 'knowledge_sources', payload: { sources: [source] } }, { artifact_type: 'mail_candidates' }]
  const result = unavailableKnowledgeArtifacts(artifacts)
  assert.deepEqual(result[0].payload.sources, [])
  assert.equal(result[1], artifacts[1])
  assert.equal(artifacts[0].payload.sources.length, 1)
})

test('来源详情不得替换旧引用的标题、版本、位置或正文', () => {
  assert.equal(sameSourceSnapshot(source, { ...source }), true)
  for (const changed of [{ title: '新标题' }, { content_hash: 'b'.repeat(64) }, { excerpt: '新正文' }, { version_id: 'other' }, { start_offset: 99 }]) {
    assert.equal(sameSourceSnapshot(source, { ...source, ...changed }), false)
  }
})

test('来源卡片转义标题和摘录，并保留版本、位置与历史编号', async () => {
  const html = await renderToString(h(Card, { artifact: { payload: { sources: [source], availability: 'partial', has_conflict: true } } }))
  assert.ok(html.includes('&lt;script&gt;') && html.includes('&lt;img'))
  assert.ok(!html.includes('<script>') && !html.includes('<img'))
  assert.ok(html.includes('[2]') && html.includes('v1') && html.includes('部分依据') && html.includes('资料存在冲突'))
})

test('失效、核验中或请求错误时不渲染缓存的旧摘录', async () => {
  for (const [availability, error] of [['unavailable', ''], ['loading', ''], ['available', '网络失败']]) {
    const html = await renderToString(h(Card, { artifact: { payload: { sources: [source], availability } }, error }))
    assert.ok(!html.includes('&lt;img') && !html.includes('&lt;script&gt;'))
  }
})

test('SSE事件去重后按需刷新，刷新失败清除旧来源，过期请求不覆盖新结果', async () => {
  setActivePinia(createPinia())
  const store = useStore()
  store.current = { conversation_id: 'conversation' }
  store.runs = [{ run_id: 'run', steps: [], tool_previews: [] }]
  const event = { run_id: 'run', event_no: 1, event_type: 'artifacts_ready', payload: { artifact_types: ['knowledge_sources'] } }
  assert.equal(store.applyRunEvent(event), true)
  assert.equal(store.applyRunEvent(event), false)
  const artifact = { artifact_type: 'knowledge_sources', payload: { sources: [source], availability: 'available' } }
  globalThis.__knowledgeArtifacts = async () => [artifact]
  await store.loadArtifacts('conversation', 'run')
  assert.equal(store.artifactsByRun.run[0].payload.sources.length, 1)
  globalThis.__knowledgeArtifacts = async () => { throw new Error('offline') }
  await assert.rejects(store.loadArtifacts('conversation', 'run'))
  assert.equal(store.artifactsByRun.run[0].payload.sources.length, 0)
  assert.ok(store.artifactErrors.run)
  let finish
  globalThis.__knowledgeArtifacts = () => new Promise((resolve) => { finish = resolve })
  const old = store.loadArtifacts('conversation', 'run')
  globalThis.__knowledgeArtifacts = async () => []
  await store.loadArtifacts('conversation', 'run')
  finish([artifact])
  await old
  assert.equal(store.artifactsByRun.run.length, 0)
})

test('固定协同步骤只推进事件游标，审批仍只由 approval_required 驱动', () => {
  setActivePinia(createPinia())
  const store = useStore()
  store.runs = [{ run_id: 'run-cooperation', status: 'running', steps: [], approval_items: [] }]
  const step = { run_id: 'run-cooperation', event_no: 1, event_type: 'cooperation_step_completed',
    payload: { version: 1, step_id: 'meeting_draft', status: 'waiting_confirmation',
      draft_id: 'draft-id', approval_item_id: 'approval-id', source: { resource_type: 'email', id: 'mail-id' } } }
  assert.equal(store.applyRunEvent(step), true)
  assert.equal(store.runEventNos['run-cooperation'], 1)
  assert.equal(store.runs[0].status, 'running')
  assert.deepEqual(store.runs[0].approval_items, [])
  assert.deepEqual(store.messages, [])
  assert.equal(store.applyRunEvent(step), false)

  assert.equal(store.applyRunEvent({ run_id: 'run-cooperation', event_no: 2,
    event_type: 'approval_required', payload: { approval_item_id: 'approval-id' } }), true)
  assert.equal(store.runs[0].status, 'waiting_confirmation')
  assert.equal(store.runs[0].approval_items[0].approval_item_id, 'approval-id')
})

test('任务失败提示随SSE、历史恢复及轮询显示，不伪造助手回复或混用其他任务', async () => {
  setActivePinia(createPinia())
  const store = useStore()
  store.current = { conversation_id: 'conversation' }
  const message = { message_id: 'user-1', role: 'user', run_id: 'failed-run', content: '安排会议草稿' }
  store.messages = [message]
  store.runs = [{ run_id: 'old-run', status: 'completed' }, { run_id: 'failed-run', status: 'running' }]
  const event = { run_id: 'failed-run', event_no: 1, event_type: 'run_failed',
    payload: { status: 'failed', error_code: 'LLM_UNAVAILABLE', error_message: 'PRIVATE_RAW_PROVIDER_TEXT' } }
  assert.equal(store.applyRunEvent(event), true)
  assert.equal(store.applyRunEvent(event), false)
  const matchingRun = () => store.runs.find(run => run.run_id === store.messages[0].run_id)
  const renderNotice = () => renderToString(h(FailureNotice, { run: matchingRun() }))
  const html = await renderNotice()
  assert.ok(html.includes('任务未完成') && html.includes('模型服务调用失败') && html.includes('failed-run'))
  assert.ok(!html.includes('PRIVATE_RAW_PROVIDER_TEXT') && !html.includes('old-run'))
  assert.equal(store.messages.length, 1)
  globalThis.__knowledgeConversationDetail = async () => ({ conversation_id: 'conversation',
    messages: [message], runs: [{ ...matchingRun() }] })
  await store.loadConversation('conversation')
  assert.ok((await renderNotice()).includes('模型服务调用失败'))
  globalThis.__knowledgeRunDetail = async () => ({ ...matchingRun(), conversation_id: 'conversation' })
  await store.refreshRun('failed-run')
  assert.ok((await renderNotice()).includes('任务未完成'))
  assert.equal(store.messages.length, 1)
})

test('失败提示不冒充成功/审批，也不渲染原始供应商错误或HTML', async () => {
  for (const status of ['running', 'queued', 'completed', 'waiting_confirmation', 'cancelled']) {
    const html = await renderToString(h(FailureNotice, { run: { run_id: 'run', status } }))
    assert.ok(!html.includes('role="alert"') && !html.includes('任务未完成'))
  }
  const html = await renderToString(h(FailureNotice, { run: { run_id: '<script>unsafe</script>', status: 'failed',
    error_code: 'UNRECOGNIZED', error_message: 'PRIVATE_BODY <img src=x onerror=alert(1)>' } }))
  assert.ok(html.includes('本次任务执行失败') && html.includes('&lt;script&gt;'))
  assert.ok(!html.includes('PRIVATE_BODY') && !html.includes('<script>') && !html.includes('<img'))
})
