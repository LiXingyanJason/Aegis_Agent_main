import { before, after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createPinia, setActivePinia } from 'pinia'

let server
let useConversationStore

before(async () => {
  server = await createServer({
    plugins: [{
      name: 'cooperation-events-test-api',
      enforce: 'pre',
      resolveId(source, importer) {
        if (importer?.replaceAll('\\', '/').endsWith('/store/conversation.js')
          && ['../api/conversation', '../api/run'].includes(source)) {
          return '\0cooperation-events-test-api'
        }
      },
      load(id) {
        if (id !== '\0cooperation-events-test-api') return null
        return `export const createConversation = () => {};
          export const getConversationArtifacts = () => {};
          export const getConversationDetail = async () => globalThis.__cooperationTestConversation;
          export const listConversations = () => {};
          export const sendConversationMessage = () => {};
          export const getRunDetail = async () => globalThis.__cooperationTestRun;`
      },
    }],
    server: { middlewareMode: true, hmr: false },
    appType: 'custom',
  })
  useConversationStore = (await server.ssrLoadModule('/src/store/conversation.js')).useConversationStore
})

after(async () => { await server?.close() })

test('协同审计事件只推进游标，不创建会议卡片或改变任务结果', () => {
  setActivePinia(createPinia())
  const store = useConversationStore()
  const run = { run_id: 'run-1', status: 'running', current_stage: '执行中', approval_items: [], plan_previews: [] }
  store.runs = [run]
  const storedRun = store.runs[0]
  store.latestSubmission = { run_id: 'run-1', status: 'running' }

  for (const [index, eventType] of [
    'cooperation_step_completed',
    'cooperation_plan_completed',
    'cooperation_schedule_step_completed',
    'cooperation_completed',
  ].entries()) {
    const eventNo = index + 1
    assert.equal(store.applyRunEvent({
      run_id: 'run-1', event_no: eventNo, event_type: eventType,
      payload: { status: 'completed', draft_id: 'untrusted', approval_item_id: 'untrusted',
        result_ref: 'cooperation_results.mail_requirements' },
    }), true)
    assert.equal(store.runEventNos['run-1'], eventNo)
    assert.strictEqual(store.runs[0], storedRun)
    assert.deepEqual(store.messages, [])
    assert.equal(store.latestSubmission.status, 'running')
  }

  assert.equal(store.applyRunEvent({
    run_id: 'run-1', event_no: 4, event_type: 'cooperation_completed', payload: {},
  }), false)
  assert.equal(store.applyRunEvent({
    run_id: 'run-1', event_no: 5, event_type: 'run_completed', payload: { status: 'completed' },
  }), true)
  assert.equal(store.runs[0].status, 'completed')
  assert.deepEqual(store.runs[0].approval_items, [])
  assert.equal(store.runs[0].approval_execution, undefined)
})

const progress = (values = {}) => ({ schema_version: 1, execution_mode: 'scheduled', stage: 'step_started',
  status: 'running', label: '日历助手正在查询空闲时间', phase: 'calendar_candidates', domain: 'calendar', step_id: 'slots', ...values })

test('实时协同进度只更新展示，重复/乱序事件不回退，不触发审批或卡片', async () => {
  setActivePinia(createPinia())
  const store = useConversationStore()
  store.runs = [{ run_id: 'run-1', status: 'running', approval_items: [], plan_previews: [] }]
  const emit = (number, payload) => store.applyRunEvent({ run_id: 'run-1', event_no: number, event_type: 'cooperation_progress', payload })
  assert.equal(emit(2, { ...progress(), draft_id: 'untrusted', approval_item_id: 'untrusted' }), true)
  assert.equal(store.runs[0].cooperation.execution_mode, 'scheduled')
  assert.equal(store.runs[0].status, 'running')
  assert.equal(store.runs[0].cooperation.draft_id, undefined)
  assert.deepEqual(store.runs[0].approval_items, [])
  assert.deepEqual(store.messages, [])
  assert.equal(emit(2, progress({ label: '重复' })), false)
  assert.equal(emit(1, progress({ label: '过期' })), false)
  assert.equal(store.runs[0].cooperation.label, '日历助手正在查询空闲时间')
  emit(3, progress({ stage: 'waiting_confirmation', status: 'waiting_confirmation', phase: 'meeting_draft', label: '会议草稿待确认' }))
  assert.equal(store.runs[0].status, 'running') // 真正任务状态只由approval_required等权威事件更新。
  emit(4, progress({ stage: 'finished', status: 'unavailable', label: '协同处理未全部完成' }))
  assert.equal(store.runs[0].status, 'running')
  assert.deepEqual(store.runs[0].plan_previews, [])
  store.applyRunEvent({ run_id: 'run-1', event_no: 5, event_type: 'run_cancelled', payload: {} })
  emit(6, progress())
  assert.equal(store.runs[0].status, 'cancelled')
})

test('刷新会话恢复协同快照；较旧轮询结果不能覆盖新SSE进度', async () => {
  setActivePinia(createPinia())
  const store = useConversationStore()
  globalThis.__cooperationTestConversation = { conversation_id: 'c1', messages: [], runs: [
    { run_id: 'run-1', status: 'running', cooperation: progress({ event_no: 10 }) },
  ] }
  await store.loadConversation('c1')
  assert.equal(store.runs[0].cooperation.step_id, 'slots')
  store.applyRunEvent({ run_id: 'run-1', event_no: 12, event_type: 'cooperation_progress',
    payload: progress({ stage: 'finished', status: 'rejected', label: '已拒绝创建会议' }) })
  globalThis.__cooperationTestRun = { run_id: 'run-1', conversation_id: 'c1', status: 'running',
    cooperation: progress({ event_no: 11 }) }
  await store.refreshRun('run-1')
  assert.equal(store.runs[0].cooperation.event_no, 12)
  assert.equal(store.runs[0].cooperation.status, 'rejected')
  delete globalThis.__cooperationTestRun
  delete globalThis.__cooperationTestConversation
})

test('终态任务不会因旧协同进度显示正在运行，固定与调度模式均可展示', async () => {
  const { cooperationDisplay } = await import('../src/utils/cooperationProgress.js')
  assert.equal(cooperationDisplay({ status: 'failed', cooperation: progress({ event_no: 1 }) }).label, '协同处理失败')
  assert.equal(cooperationDisplay({ status: 'cancelled', cooperation: progress({ event_no: 1 }) }).active, false)
  assert.equal(cooperationDisplay({ status: 'completed', cooperation: progress({ event_no: 1 }) }).active, false)
  assert.equal(cooperationDisplay({ status: 'waiting_confirmation', cooperation: progress({ event_no: 1 }) }).label, '会议草稿待确认')
  assert.equal(cooperationDisplay({ status: 'running', cooperation: progress({ event_no: 1, execution_mode: 'fixed' }) }).mode_label, '固定协同')
  assert.equal(cooperationDisplay({ status: 'running', cooperation: progress({ event_no: 1 }) }).mode_label, '计划调度协同')
  assert.equal(cooperationDisplay({ status: 'running' }), null)
})

test('进度组件渲染模式及等待提示，不渲染原始HTML或执行按钮', async () => {
  const { createSSRApp } = await import('vue')
  const { renderToString } = await import('@vue/server-renderer')
  const Component = (await server.ssrLoadModule('/src/components/CooperationProgress.vue')).default
  const html = await renderToString(createSSRApp(Component, { run: { status: 'waiting_confirmation',
    cooperation: progress({ event_no: 1, label: '<script>bad</script>' }) } }))
  assert.match(html, /计划调度协同/)
  assert.match(html, /会议草稿待确认/)
  assert.doesNotMatch(html, /<button|<script>/)
})

test('阶段8旧版本与路由守卫失败给出兼容提示，不显示后端原文或自动重试', async () => {
  const { createSSRApp } = await import('vue')
  const { renderToString } = await import('@vue/server-renderer')
  const Component = (await server.ssrLoadModule('/src/components/RunFailureNotice.vue')).default
  const { runFailureReason, VERSION_INCOMPATIBLE_MESSAGE } = await import('../src/utils/runFailure.js')
  for (const [errorCode, errorMessage] of [
    ['POLICY_VERSION_UNAVAILABLE', 'old policy detail'],
    ['WORKFLOW_ROUTING_FAILED', 'scheduler_version_mismatch'],
    ['OTHER_GUARD', 'plan_binding_changed'],
    ['OTHER_GUARD', 'cooperation_restart_blocked'],
  ]) {
    const run = { run_id: 'old-run', status: 'failed', error_code: errorCode, error_message: errorMessage }
    assert.equal(runFailureReason(run), VERSION_INCOMPATIBLE_MESSAGE)
    const html = await renderToString(createSSRApp(Component, { run }))
    assert.match(html, /任务版本不兼容/)
    assert.match(html, /不会自动恢复、重试或沿用原批准/)
    assert.doesNotMatch(html, /old policy detail|scheduler_version_mismatch|plan_binding_changed|cooperation_restart_blocked/)
    assert.doesNotMatch(html, /<button/)
  }
  assert.equal(runFailureReason({ status: 'failed', error_code: 'LLM_UNAVAILABLE' }),
    '模型服务调用失败，本次任务未能完成。')
})

test('旧 fixed 快照只读保留，新 scheduled 进度不被审计版本字段覆盖', () => {
  setActivePinia(createPinia())
  const store = useConversationStore()
  store.runs = [{ run_id: 'old-run', status: 'completed', cooperation: progress({ execution_mode: 'fixed', event_no: 7 }) },
    { run_id: 'new-run', status: 'running', cooperation: progress({ event_no: 2 }) }]
  assert.equal(store.applyRunEvent({ run_id: 'new-run', event_no: 3, event_type: 'cooperation_plan_completed',
    payload: { catalog_version: 'cooperation-catalog-v2', policy_version: 'harness-v6-cooperation', scheduler_version: 'cooperation-scheduler-v2' } }), true)
  assert.equal(store.runs[0].cooperation.execution_mode, 'fixed')
  assert.equal(store.runs[1].cooperation.execution_mode, 'scheduled')
  assert.equal(store.runs[1].cooperation.event_no, 2)
  assert.equal(store.runEventNos['new-run'], 3)
})
