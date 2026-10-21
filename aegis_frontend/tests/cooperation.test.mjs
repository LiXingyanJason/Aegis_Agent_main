import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createPinia, setActivePinia } from 'pinia'
import { cooperationStageLabel, groupToolPreviews, inputKindHint, inputKindTitle } from '../src/utils/cooperation.js'
import { buildInputAnswerPayload, inputAnswerKey, inputRequestBelongsToScope, inputRequestType, pendingInputRunsAfterMessage, unanchoredPendingInputRuns } from '../src/utils/inputRequest.js'
import { newestRunsFirst } from '../src/utils/runOrder.js'
import { runProgressText } from '../src/utils/runProgress.js'

test('对话内进度优先使用真实工具与步骤状态，终态不显示运行提示', () => {
  assert.equal(runProgressText({ status: 'queued' }), '任务已提交，等待开始')
  assert.equal(runProgressText({ status: 'running', current_stage: '等待处理', steps: [
    { label: '查询相关邮件', status: 'running' },
  ] }), '正在处理：查询相关邮件')
  assert.equal(runProgressText({ status: 'running', tool_previews: [
    { tool_name: 'mail.messages.list', status: 'running' },
  ] }), '正在调用工具：mail.messages.list')
  assert.equal(runProgressText({ status: 'running' }, [{ phase: 'expert_started', cooperation: { actor: 'mail' } }]), '邮件专家：专家正在处理')
  assert.equal(runProgressText({ status: 'waiting_input' }), '等待你补充信息后继续')
  assert.equal(runProgressText({ status: 'completed', steps: [{ label: '查询邮件', status: 'running' }] }), '')
})

test('执行进度按提交时间由近到远排列，不改变原运行数组', () => {
  const runs = [
    { run_id: 'old', created_at: '2026-10-19T13:24:00Z', finished_at: '2026-10-21T10:00:00Z' },
    { run_id: 'new', created_at: '2026-10-20T13:24:00Z' },
    { run_id: 'middle', created_at: '2026-10-19T13:35:00Z' },
  ]
  assert.deepEqual(newestRunsFirst(runs).map((run) => run.run_id), ['new', 'middle', 'old'])
  assert.deepEqual(runs.map((run) => run.run_id), ['old', 'new', 'middle'])
})

let server, useConversationStore
before(async () => {
  server = await createServer({ plugins: [{
    name: 'cooperation-test-api', enforce: 'pre',
    resolveId(source, importer) {
      if (importer?.replaceAll('\\', '/').endsWith('/store/conversation.js')
        && ['../api/conversation', '../api/run'].includes(source)) return '\0cooperation-mock-api'
    },
    load(id) {
      if (id !== '\0cooperation-mock-api') return null
      return `export const createConversation = () => {}; export const getConversationArtifacts = () => {};
        export const getConversationDetail = () => {}; export const listConversations = () => {};
        export const sendConversationMessage = () => {}; export const getRunDetail = () => {};`
    },
  }], server: { middlewareMode: true, hmr: false }, appType: 'custom' })
  useConversationStore = (await server.ssrLoadModule('/src/store/conversation.js')).useConversationStore
})
after(async () => { await server?.close() })

function storeWithRun(status = 'running') {
  setActivePinia(createPinia())
  const store = useConversationStore()
  store.runs = [{ run_id: 'run-1', status, steps: [], tool_previews: [], approval_items: [] }]
  return store
}

test('补问类型只影响标题，未知类型安全回退', () => {
  assert.equal(inputKindTitle('mail_selection'), '请选择用于安排会议的邮件')
  assert.equal(inputKindTitle('calendar_requirements'), '请补充或核对会议安排要求')
  assert.equal(inputKindTitle('future_kind'), '需要补充信息')
  assert.match(inputKindHint('mail_selection', 'single_choice'), /选择一封邮件/)
})

test('结构化单选只提交当前快照中的选项ID，不默认选择或混传文本', () => {
  const item = { question_id: 'q1', version: 2, input_type: 'single_choice',
    options: [{ id: 'choice-1', label: '第一封' }, { id: 'choice-2', label: '第二封' }] }
  assert.equal(buildInputAnswerPayload(item, {}, 'client-1'), null)
  assert.equal(buildInputAnswerPayload(item, { response: '第一封' }, 'client-1'), null)
  assert.equal(buildInputAnswerPayload(item, { selectedOptionId: 'unknown' }, 'client-1'), null)
  const payload = buildInputAnswerPayload(item, { selectedOptionId: 'choice-2', response: '第一封' }, 'client-1')
  assert.deepEqual(payload, { expected_version: 2, client_response_id: 'client-1', selected_option_id: 'choice-2' })
  assert.equal(inputAnswerKey(item, payload), 'q1:2:single_choice:choice-2')
})

test('旧问题和开放式问题仍仅提交文本，未知类型禁用', () => {
  const legacy = { question_id: 'q2', version: 1 }
  assert.equal(inputRequestType(legacy), 'text')
  assert.deepEqual(buildInputAnswerPayload(legacy, { response: ' 会议时长50分钟 ' }, 'client-2'),
    { expected_version: 1, client_response_id: 'client-2', response: '会议时长50分钟' })
  assert.equal(buildInputAnswerPayload(legacy, { selectedOptionId: 'choice-1' }, 'client-2'), null)
  assert.equal(inputRequestType({ input_type: 'future' }), 'unsupported')
})

test('会话补问只归当前对话；邮件管理仅显示无会话的邮件任务', () => {
  const fromDetail = new Map([['main-run', 'conversation-1'], ['mail-run', null]])
  const main = { scope: 'conversation', conversationId: 'conversation-1', runIds: ['known-run'] }
  const mail = { scope: 'mail' }
  assert.equal(inputRequestBelongsToScope({ run_id: 'known-run' }, main, fromDetail), true)
  assert.equal(inputRequestBelongsToScope({ run_id: 'main-run' }, main, fromDetail), true)
  assert.equal(inputRequestBelongsToScope({ run_id: 'mail-run' }, main, fromDetail), false)
  assert.equal(inputRequestBelongsToScope({ run_id: 'main-run' }, mail, fromDetail), false)
  assert.equal(inputRequestBelongsToScope({ run_id: 'mail-run' }, mail, fromDetail), true)
  assert.equal(inputRequestBelongsToScope({ run_id: 'unknown-run' }, mail, fromDetail), false)
})

test('待答卡片只跟随所属运行的最后一条消息，不占据消息区外高度', () => {
  const messages = [
    { message_id: 'm1', run_id: 'run-1' },
    { message_id: 'm2', run_id: 'run-2' },
    { message_id: 'm3', run_id: 'run-1' },
  ]
  const runs = [
    { run_id: 'run-1', status: 'waiting_input' },
    { run_id: 'run-2', status: 'completed' },
    { run_id: 'run-3', status: 'waiting_input' },
  ]
  assert.deepEqual(pendingInputRunsAfterMessage(messages, runs, 0), [])
  assert.deepEqual(pendingInputRunsAfterMessage(messages, runs, 1), [])
  assert.deepEqual(pendingInputRunsAfterMessage(messages, runs, 2).map((run) => run.run_id), ['run-1'])
  assert.deepEqual(unanchoredPendingInputRuns(messages, runs).map((run) => run.run_id), ['run-3'])
})

test('团队生成结束不改变任务状态，按逻辑ID与事件号去重', () => {
  const store = storeWithRun()
  const event = { run_id: 'run-1', event_no: 1, event_type: 'cooperation_progress',
    payload: { phase: 'finished', cooperation_event_id: 'logical-1', cooperation: { team_id: 'team-1', actor: 'coordinator' } } }
  assert.equal(store.applyRunEvent(event), true)
  assert.equal(store.runs[0].status, 'running')
  assert.equal(store.applyRunEvent({ ...event, event_no: 2 }), true)
  assert.equal(store.applyRunEvent(event), false)
  assert.equal(store.cooperationByRun['run-1'].length, 1)
  assert.equal(cooperationStageLabel(store.cooperationByRun['run-1'][0]), '团队生成阶段结束')
})

test('历史回放只补协作关联，不回退权威任务终态', () => {
  const store = storeWithRun('completed')
  store.runs[0].tool_previews = [{ tool_call_id: 'tool-1', tool_name: 'calendar.find', status: 'succeeded' }]
  store.applyRunEvent({ run_id: 'run-1', event_no: 1, event_type: 'run_started', payload: {} }, { historyOnly: true })
  store.applyRunEvent({ run_id: 'run-1', event_no: 2, event_type: 'tool_preview',
    payload: { tool_call_id: 'tool-1', cooperation: { team_id: 'team-1', actor: 'calendar', delegation_id: 'd1', sequence: 1 } } }, { historyOnly: true })
  assert.equal(store.runs[0].status, 'completed')
  assert.equal(groupToolPreviews(store.runs[0].tool_previews)[0].label, '日历专家 · 委派 1')
})

test('同一团队的多次专家委派不会把工具调用混为一组', () => {
  const tools = [
    { cooperation: { team_id: 't', actor: 'mail', delegation_id: 'd1', sequence: 1 } },
    { cooperation: { team_id: 't', actor: 'mail', delegation_id: 'd2', sequence: 2 } },
    {},
  ]
  assert.equal(groupToolPreviews(tools).length, 3)
})

test('补问与取消沿用原任务事件，不能由团队阶段判定任务终态', () => {
  const store = storeWithRun()
  store.applyRunEvent({ run_id: 'run-1', event_no: 1, event_type: 'input_required', payload: { question_id: 'q1' } })
  assert.equal(store.runs[0].status, 'waiting_input')
  store.applyRunEvent({ run_id: 'run-1', event_no: 2, event_type: 'cooperation_progress', payload: { phase: 'resumed' } })
  assert.equal(store.runs[0].status, 'running')
  store.applyRunEvent({ run_id: 'run-1', event_no: 3, event_type: 'run_cancelled', payload: { status: 'cancelled' } })
  assert.equal(store.runs[0].status, 'cancelled')
})
