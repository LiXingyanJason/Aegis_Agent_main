<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  createMailTodoDrafts,
  getMailMessageDetail,
  requestMailExtraction,
  requestMailReplyDraft,
} from '../api/mail'
import { getRunDetail, subscribeRunEvents } from '../api/run'

const props = defineProps({ artifact: { type: Object, required: true } })
const router = useRouter()
const payload = computed(() => props.artifact.payload ?? {})
const items = computed(() => Array.isArray(payload.value.items) ? payload.value.items : [])
const isSingleCard = computed(() => payload.value.display_mode === 'single' && items.value.length === 1)
const operations = ref({})
const extractions = ref({})
const selectedTodos = ref({})
const instructions = ref({})
const detailVisible = ref(false)
const detailLoading = ref(false)
const detailError = ref('')
const detail = ref(null)
const selectedItem = ref(null)
const streams = new Map()
const retryTimers = new Map()
const pollTimers = new Map()
const eventNos = new Map()
let active = true
let detailRequestId = 0

function messageFromError(cause, fallback) {
  const detail = cause.response?.data?.detail
  return (typeof detail === 'object' ? detail?.message : detail) || cause.message || fallback
}

function formatTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: 'numeric', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  }).format(date)
}

function extractionStatus(status) {
  return {
    not_generated: '尚未生成摘要',
    running: '摘要生成中',
    completed: '已有摘要',
    stale: '邮件已更新，摘要需重新生成',
    failed: '摘要生成失败',
  }[status] ?? status ?? '尚未生成摘要'
}

function operationFor(emailId) {
  return operations.value[emailId] ?? null
}

function isBusy(emailId) {
  return ['submitting', 'running'].includes(operationFor(emailId)?.status)
}

function setOperation(emailId, operation) {
  if (active) operations.value = { ...operations.value, [emailId]: operation }
}

function extractionFor(emailId) {
  return extractions.value[emailId] ?? null
}

function applyExtraction(emailId, result) {
  if (!active) return
  extractions.value = {
    ...extractions.value,
    [emailId]: {
      extraction_id: result.extraction_id,
      key_points: result.key_points ?? [],
      todos: result.todos ?? [],
      due_date_candidates: result.due_date_candidates ?? [],
    },
  }
}

function selectedIndexes(emailId) {
  return selectedTodos.value[emailId] ?? []
}

function toggleTodo(emailId, index, checked) {
  const next = new Set(selectedIndexes(emailId))
  if (checked) next.add(index)
  else next.delete(index)
  selectedTodos.value = { ...selectedTodos.value, [emailId]: [...next] }
}

function stopRun(runId) {
  streams.get(runId)?.abort()
  streams.delete(runId)
  if (retryTimers.has(runId)) window.clearTimeout(retryTimers.get(runId))
  retryTimers.delete(runId)
  if (pollTimers.has(runId)) window.clearInterval(pollTimers.get(runId))
  pollTimers.delete(runId)
}

function stopAllRuns() {
  for (const runId of new Set([...streams.keys(), ...retryTimers.keys(), ...pollTimers.keys()])) stopRun(runId)
}

async function pollRun(runId, emailId, kind) {
  if (!active || !pollTimers.has(runId)) return
  try {
    const run = await getRunDetail(runId)
    if (!active || !pollTimers.has(runId)) return
    if (run.status === 'failed') {
      setOperation(emailId, { kind, status: 'failed', message: run.error_message || '邮件任务执行失败。' })
      stopRun(runId)
    } else if (run.status === 'completed') {
      setOperation(emailId, {
        kind, status: 'completed',
        message: kind === 'extraction'
          ? '摘要任务已完成；结果未通过实时连接送达，请到邮件管理页查看。'
          : '回复草稿已保存，请到邮件管理页的“保存草稿”查看。',
      })
      stopRun(runId)
    }
  } catch {
    // 保留下一次轮询；不将暂时的网络失败误报为任务失败。
  }
}

function startPolling(runId, emailId, kind) {
  if (pollTimers.has(runId)) return
  pollTimers.set(runId, window.setInterval(() => { void pollRun(runId, emailId, kind) }, 1500))
  void pollRun(runId, emailId, kind)
}

function scheduleReconnect(runId, emailId, kind) {
  if (retryTimers.has(runId) || !active || operationFor(emailId)?.status !== 'running') return
  startPolling(runId, emailId, kind)
  retryTimers.set(runId, window.setTimeout(() => {
    retryTimers.delete(runId)
    void streamRun(runId, emailId, kind)
  }, 2500))
}

async function streamRun(runId, emailId, kind) {
  if (!active || streams.has(runId) || operationFor(emailId)?.status !== 'running') return
  const controller = new AbortController()
  streams.set(runId, controller)
  try {
    await subscribeRunEvents(runId, {
      lastEventId: eventNos.get(runId) ?? 0,
      signal: controller.signal,
      onOpen: () => {
        if (pollTimers.has(runId)) window.clearInterval(pollTimers.get(runId))
        pollTimers.delete(runId)
      },
      onEvent: async (event) => {
        if (!active || controller.signal.aborted) return
        const eventNo = Number(event.data?.event_no ?? event.id)
        if (Number.isSafeInteger(eventNo)) {
          if (eventNo <= (eventNos.get(runId) ?? 0)) return
          eventNos.set(runId, eventNo)
        }
        const eventPayload = event.data?.payload ?? {}
        if (event.event === 'mail_extraction_completed' && kind === 'extraction') {
          applyExtraction(emailId, eventPayload)
          setOperation(emailId, { kind, status: 'completed', message: '摘要已生成，可核对待办候选。' })
          stopRun(runId)
        } else if (event.event === 'mail_reply_draft_completed' && kind === 'reply') {
          setOperation(emailId, { kind, status: 'completed', draftId: eventPayload.draft_id, message: '回复草稿已保存，尚未发送。' })
          stopRun(runId)
        } else if (event.event === 'run_failed') {
          setOperation(emailId, { kind, status: 'failed', message: eventPayload.error_message || eventPayload.message || '邮件任务执行失败。' })
          stopRun(runId)
        } else if (event.event === 'run_completed' && operationFor(emailId)?.status === 'running') {
          setOperation(emailId, {
            kind, status: 'completed',
            message: kind === 'extraction' ? '摘要任务已完成，请到邮件管理页查看结果。' : '回复草稿已保存，请到邮件管理页查看。',
          })
          stopRun(runId)
        }
      },
    })
  } catch {
    if (!controller.signal.aborted) startPolling(runId, emailId, kind)
  } finally {
    if (streams.get(runId) === controller) streams.delete(runId)
    if (!controller.signal.aborted) scheduleReconnect(runId, emailId, kind)
  }
}

async function openDetail(item) {
  const requestId = ++detailRequestId
  selectedItem.value = item
  detailVisible.value = true
  detailLoading.value = true
  detailError.value = ''
  detail.value = null
  try {
    const result = await getMailMessageDetail(item.email_id)
    if (active && requestId === detailRequestId) detail.value = result
  } catch (cause) {
    if (active && requestId === detailRequestId) detailError.value = messageFromError(cause, '读取邮件详情失败。')
  } finally {
    if (active && requestId === detailRequestId) detailLoading.value = false
  }
}

async function generateSummary(item) {
  const emailId = item.email_id
  if (isBusy(emailId)) return
  setOperation(emailId, { kind: 'extraction', status: 'submitting', message: '' })
  try {
    const result = await requestMailExtraction(emailId)
    if (!active) return
    if (result.cached && result.extraction) {
      applyExtraction(emailId, { extraction_id: result.extraction_id, ...result.extraction })
      setOperation(emailId, { kind: 'extraction', status: 'completed', message: '已读取现有摘要，可核对待办候选。' })
    } else if (result.run_id) {
      setOperation(emailId, { kind: 'extraction', status: 'running', message: '摘要任务已提交，正在处理…' })
      void streamRun(result.run_id, emailId, 'extraction')
    } else throw new Error('摘要任务未返回运行标识。')
  } catch (cause) {
    setOperation(emailId, { kind: 'extraction', status: 'failed', message: messageFromError(cause, '提交摘要任务失败。') })
  }
}

async function addTodos(item) {
  const emailId = item.email_id
  const extraction = extractionFor(emailId)
  const indexes = selectedIndexes(emailId)
  if (!extraction?.extraction_id || !indexes.length || isBusy(emailId)) return
  setOperation(emailId, { kind: 'todo', status: 'submitting', message: '' })
  try {
    const drafts = await createMailTodoDrafts(emailId, { extraction_id: extraction.extraction_id, todo_indexes: indexes })
    if (!active) return
    selectedTodos.value = { ...selectedTodos.value, [emailId]: [] }
    setOperation(emailId, { kind: 'todo', status: 'completed', message: `已加入 ${drafts.length} 项站内待办草稿。` })
  } catch (cause) {
    setOperation(emailId, { kind: 'todo', status: 'failed', message: messageFromError(cause, '加入待办草稿失败。') })
  }
}

async function draftReply(item) {
  const emailId = item.email_id
  if (isBusy(emailId)) return
  setOperation(emailId, { kind: 'reply', status: 'submitting', message: '' })
  try {
    const extraction = extractionFor(emailId)
    const instruction = instructions.value[emailId]?.trim()
    const result = await requestMailReplyDraft(emailId, {
      ...(extraction?.extraction_id ? { extraction_id: extraction.extraction_id } : {}),
      ...(instruction ? { instruction } : {}),
    })
    if (!active) return
    if (!result.run_id) throw new Error('回复草稿任务未返回运行标识。')
    setOperation(emailId, { kind: 'reply', status: 'running', message: '回复草稿任务已提交，正在处理…' })
    void streamRun(result.run_id, emailId, 'reply')
  } catch (cause) {
    setOperation(emailId, { kind: 'reply', status: 'failed', message: messageFromError(cause, '提交回复草稿任务失败。') })
  }
}

function openDraft(draftId) {
  return router.push({ name: 'mail-management', query: { draft_id: draftId } })
}

onBeforeUnmount(() => {
  active = false
  ++detailRequestId
  stopAllRuns()
})
</script>

<template>
  <section class="mail-candidates-card" :class="{ 'mail-candidates-single': isSingleCard }" :aria-label="isSingleCard ? '邮件操作卡片' : '邮件查询候选'">
    <div v-if="!isSingleCard" class="mail-candidates-heading">
      <div>
        <b>邮件查询结果</b>
        <span v-if="payload.source === 'mock_mailbox'" class="mail-candidates-source">模拟邮箱</span>
      </div>
      <span class="mail-candidates-count">匹配 {{ payload.matched_count ?? 0 }} 封 · 展示 {{ payload.returned_count ?? items.length }} 封</span>
    </div>
    <p v-if="payload.truncated" class="mail-candidates-truncated">结果已截断，仅展示前 {{ payload.returned_count ?? items.length }} 封；匹配总数为 {{ payload.matched_count }} 封。</p>
    <p v-if="!items.length" class="mail-candidates-empty">{{ payload.matched_count === 0 ? '没有匹配邮件。' : '本次没有可展示的候选，请调整查询条件。' }}</p>
    <article v-for="item in items" :key="item.email_id" class="mail-candidate-item">
      <div class="mail-candidate-main">
        <span class="mail-candidate-position">{{ item.position }}</span>
        <div class="mail-candidate-info">
          <strong>{{ item.subject || '（无主题）' }}</strong>
          <span>{{ item.sender_name || item.sender_email }}<template v-if="item.sender_name"> · {{ item.sender_email }}</template></span>
          <small>{{ formatTime(item.received_at) }} · {{ extractionStatus(item.extraction_status) }}</small>
        </div>
      </div>
      <div class="mail-candidate-actions">
        <el-button size="small" @click="openDetail(item)">查看邮件</el-button>
        <el-button size="small" :loading="operationFor(item.email_id)?.kind === 'extraction' && isBusy(item.email_id)" :disabled="isBusy(item.email_id)" @click="generateSummary(item)">生成摘要</el-button>
        <el-button size="small" :loading="operationFor(item.email_id)?.kind === 'todo' && isBusy(item.email_id)" :disabled="!extractionFor(item.email_id)?.todos.length || !selectedIndexes(item.email_id).length || isBusy(item.email_id)" @click="addTodos(item)">加入待办草稿</el-button>
        <el-button size="small" :loading="operationFor(item.email_id)?.kind === 'reply' && isBusy(item.email_id)" :disabled="isBusy(item.email_id)" @click="draftReply(item)">起草回复</el-button>
        <router-link class="mail-candidate-link" :to="{ name: 'mail-management', query: { email_id: item.email_id } }">在邮件管理中打开</router-link>
      </div>
      <el-input v-model="instructions[item.email_id]" class="mail-candidate-instruction" size="small" placeholder="可选：补充回复要求" maxlength="2000" />
      <div v-if="extractionFor(item.email_id)" class="mail-candidate-extraction">
        <b>摘要与待办候选</b>
        <p v-for="(point, index) in extractionFor(item.email_id).key_points" :key="index">{{ point }}</p>
        <p v-if="!extractionFor(item.email_id).key_points.length" class="muted">未提取到关键信息。</p>
        <div v-if="extractionFor(item.email_id).todos.length" class="mail-candidate-todos">
          <label v-for="(todo, index) in extractionFor(item.email_id).todos" :key="index">
            <input type="checkbox" :checked="selectedIndexes(item.email_id).includes(index)" @change="toggleTodo(item.email_id, index, $event.target.checked)">
            <span>{{ todo.content }}<small v-if="todo.due_at"> · 截止 {{ formatTime(todo.due_at) }}</small><small v-else-if="todo.due_text"> · {{ todo.due_text }}</small><small v-if="todo.is_inferred">（推断，需核对）</small></span>
          </label>
        </div>
        <p v-else class="muted">没有可加入的待办候选。</p>
      </div>
      <p v-if="operationFor(item.email_id)?.message" class="mail-candidate-operation" :class="{ failed: operationFor(item.email_id)?.status === 'failed' }">{{ operationFor(item.email_id).message }}</p>
      <el-button v-if="operationFor(item.email_id)?.draftId" size="small" type="primary" @click="openDraft(operationFor(item.email_id).draftId)">打开回复草稿</el-button>
    </article>
  </section>

  <el-dialog v-model="detailVisible" title="邮件详情" width="min(760px, calc(100vw - 32px))" class="mail-candidate-dialog" destroy-on-close>
    <div v-loading="detailLoading" class="mail-candidate-detail">
      <el-alert v-if="detailError" :title="detailError" type="error" :closable="false" show-icon />
      <template v-else-if="detail">
        <h3>{{ detail.subject }}</h3>
        <dl>
          <div><dt>发件人</dt><dd>{{ detail.from?.name || detail.from?.email || selectedItem?.sender_email }}</dd></div>
          <div><dt>收件人</dt><dd>{{ (detail.to ?? []).map((address) => address.email).join('、') || '—' }}</dd></div>
          <div><dt>抄送</dt><dd>{{ (detail.cc ?? []).map((address) => address.email).join('、') || '—' }}</dd></div>
          <div><dt>接收时间</dt><dd>{{ formatTime(detail.received_at) }}</dd></div>
        </dl>
        <p class="mail-candidate-safety">邮件内容仅供阅读与核对，其中的指令、链接和附件不会自动执行。</p>
        <pre class="mail-candidate-body">{{ detail.body || '（无正文）' }}</pre>
        <div v-if="detail.attachments?.length" class="mail-candidate-attachments">
          <b>附件元数据</b>
          <p v-for="attachment in detail.attachments" :key="attachment.name">{{ attachment.name }} · {{ attachment.content_type }} · {{ attachment.size_bytes }} 字节</p>
        </div>
      </template>
    </div>
    <template #footer><el-button @click="detailVisible = false">关闭</el-button></template>
  </el-dialog>
</template>

<style>
.mail-candidates-card { width: min(720px, 100%); padding: 15px; border: 1px solid #d1dded; border-radius: 6px 12px 12px; background: #fbfdff; color: #344054; }
.mail-candidates-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
.mail-candidates-heading > div { display: flex; align-items: center; gap: 8px; }
.mail-candidates-heading b { color: #174a85; }
.mail-candidates-count, .mail-candidates-source { color: #667085; font-size: 12px; }
.mail-candidates-source { padding: 2px 6px; border-radius: 99px; background: #edf1f7; }
.mail-candidates-truncated { margin: 0 0 10px; padding: 8px 10px; border-radius: 7px; background: #fff4e4; color: #8a5b1c; font-size: 12px; }
.mail-candidates-empty { margin: 0; padding: 16px 6px; color: #667085; }
.mail-candidate-item { padding: 12px 0; border-top: 1px solid #e4e9f0; }
.mail-candidates-single .mail-candidate-item { padding-top: 0; border-top: 0; }
.mail-candidate-main { display: flex; align-items: flex-start; gap: 10px; }
.mail-candidate-position { display: grid; width: 25px; height: 25px; place-items: center; flex: none; border-radius: 50%; background: #eaf3ff; color: #1c5ba7; font-size: 12px; font-weight: 700; }
.mail-candidate-info { display: grid; min-width: 0; gap: 3px; }
.mail-candidate-info strong, .mail-candidate-info span { overflow-wrap: anywhere; }
.mail-candidate-info strong { color: #1f354d; }
.mail-candidate-info span, .mail-candidate-info small { color: #667085; font-size: 12px; }
.mail-candidate-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin: 10px 0 0 35px; }
.mail-candidate-actions .el-button + .el-button { margin-left: 0; }
.mail-candidate-link { color: #266dcc; font-size: 12px; text-decoration: none; }
.mail-candidate-link:hover { text-decoration: underline; }
.mail-candidate-instruction { max-width: 330px; margin: 9px 0 0 35px; }
.mail-candidate-extraction { margin: 12px 0 0 35px; padding: 11px; border-radius: 8px; background: #f3f7fc; font-size: 12px; }
.mail-candidate-extraction > p { margin: 7px 0; }
.mail-candidate-todos { display: grid; gap: 7px; margin-top: 10px; }
.mail-candidate-todos label { display: flex; gap: 7px; align-items: flex-start; line-height: 1.5; }
.mail-candidate-todos input { margin-top: 3px; }
.mail-candidate-todos small { color: #667085; }
.mail-candidate-todos .el-button { width: max-content; }
.mail-candidate-operation { margin: 9px 0 0 35px; color: #236b4b; font-size: 12px; line-height: 1.5; }
.mail-candidate-operation.failed { color: #b42318; }
.mail-candidate-item > .el-button { margin: 9px 0 0 35px; }
.mail-candidate-dialog { display: flex; max-height: calc(100vh - 48px); flex-direction: column; margin: 24px auto !important; overflow: hidden; }
.mail-candidate-dialog .el-dialog__header, .mail-candidate-dialog .el-dialog__footer { flex: none; }
.mail-candidate-dialog .el-dialog__body { min-height: 0; overflow-y: auto; }
.mail-candidate-detail h3 { margin: 0 0 14px; }
.mail-candidate-detail dl { margin: 0; }
.mail-candidate-detail dl div { display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 8px; padding: 7px 0; }
.mail-candidate-detail dt { color: #667085; }
.mail-candidate-detail dd { margin: 0; overflow-wrap: anywhere; }
.mail-candidate-safety { margin: 14px 0; padding: 11px; border-radius: 8px; background: #fff4e4; color: #76470c; font-size: 12px; }
.mail-candidate-body { max-height: 320px; padding: 12px; overflow: auto; border-radius: 8px; background: #f7f9fc; color: #344054; font: inherit; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }
.mail-candidate-attachments p { margin: 6px 0; color: #667085; font-size: 12px; }
@media (max-width: 600px) { .mail-candidate-actions, .mail-candidate-instruction, .mail-candidate-extraction, .mail-candidate-operation, .mail-candidate-item > .el-button { margin-left: 0; } }
</style>
