<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  getMailDraft,
  getMailMessageDetail,
  listMailDrafts,
  listMailMessages,
  listSentMailMessages,
  createMailTodoDrafts,
  requestMailExtraction,
  requestMailReplyDraft,
  requestMailSendConfirmation,
  updateMailDraft,
  requestDraftRevision,
  setMailImportance,
  setSentMailFollowUp,
} from '../api/mail'
import { useAuthStore } from '../store/auth'
import { subscribeRunEvents, getRunDetail } from '../api/run'
import RunInputPanel from '../components/RunInputPanel.vue'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const activeTab = ref('received')
const messages = ref([])
const drafts = ref([])
const sentMessages = ref([])
const snapshotAt = ref(null)
const loading = ref(false)
const mailState = ref('loading')
const error = ref('')
const draftsLoaded = ref(false)
const sentLoaded = ref(false)
const draftsError = ref('')
const sentError = ref('')
const flagBusyId = ref(null)
const flagError = ref('')
const draftDialogVisible = ref(false)
const draftDetailLoading = ref(false)
const draftSaving = ref(false)
const draftError = ref('')
const draftForm = ref(null)
const aiInstruction = ref('')
const aiRevising = ref(false)
let revisionAbort
let savedDraftSnapshot = ''
let revisionRequest = null
const resumedPolls = new Map()

function stopBackgroundPoll(runId) {
  if (resumedPolls.has(runId)) window.clearInterval(resumedPolls.get(runId))
  resumedPolls.delete(runId)
}

function startBackgroundPoll(runId) {
  if (resumedPolls.has(runId)) return
  async function check() {
    try {
      const result = await getRunDetail(runId)
      if (['completed', 'failed', 'cancelled', 'waiting_input'].includes(result.status)) {
        stopBackgroundPoll(runId)
        if (result.status === 'completed') await loadDrafts()
        if (result.status === 'failed') draftsError.value = result.error_message || '起草失败。'
        for (const [emailId, operation] of Object.entries(mailOperations.value)) {
          if (operation.runId !== runId) continue
          setOperation(emailId, {
            ...operation, status: result.status,
            error: result.status === 'failed' ? result.error_message || '邮件任务执行失败。' : '',
          })
        }
      }
    } catch { /* 短暂网络失败保留下次查询。 */ }
  }
  resumedPolls.set(runId, window.setInterval(() => { void check() }, 1500))
  void check()
}

function resumeBackground(runId) {
  const entry = Object.entries(mailOperations.value).find(([, operation]) => operation.runId === runId)
  if (entry) {
    const [emailId, operation] = entry
    setOperation(emailId, { ...operation, status: 'running', error: '' })
    void streamMailRun(runId, emailId, operation.type)
  } else {
    startBackgroundPoll(runId)
  }
}

function cancelBackground(runId) {
  streamControllers.get(runId)?.abort()
  streamControllers.delete(runId)
  stopBackgroundPoll(runId)
  for (const [emailId, operation] of Object.entries(mailOperations.value)) {
    if (operation.runId === runId) setOperation(emailId, { ...operation, status: 'failed', error: '起草任务已取消或补充问题已过期。' })
  }
}

async function waitForDraftRevision(runId, draftId, signal) {
  const streamController = new AbortController()
  const abortStream = () => streamController.abort()
  signal.addEventListener('abort', abortStream, { once: true })
  const streamTimeout = window.setTimeout(abortStream, 15000)
  let completed = false
  let failure = ''
  try {
    await subscribeRunEvents(runId, {
      signal: streamController.signal,
      onEvent: (event) => {
        const payload = event.data?.payload ?? {}
        if (event.event === 'mail_draft_revision_completed' && payload.draft_id === draftId) {
          completed = true
          streamController.abort()
        } else if (event.event === 'run_failed') {
          failure = payload.error_message || payload.message || '草稿修改失败。'
          streamController.abort()
        }
      },
    })
  } catch {
    // SSE 不可用时仍可通过任务状态完成本次修改。
  } finally {
    window.clearTimeout(streamTimeout)
    signal.removeEventListener('abort', abortStream)
  }
  if (failure) throw new Error(failure)
  if (completed || signal.aborted) return completed

  const start = Date.now()
  while (!signal.aborted) {
    const result = await getRunDetail(runId)
    if (result.status === 'completed') return true
    if (['failed', 'cancelled'].includes(result.status)) throw new Error(result.error_message || '草稿修改未完成。')
    if (Date.now() - start > 180000) throw new Error('尚未收到修改结果，请稍后刷新草稿。')
    await new Promise((resolve) => window.setTimeout(resolve, 1500))
  }
  return false
}

async function reviseDraft() {
  if (!draftForm.value || !aiInstruction.value.trim() || aiRevising.value) return
  // 先保存手工编辑，模型只能基于服务端最新版本修改。
  if (!await saveDraft()) return
  aiRevising.value = true
  draftError.value = ''
  const draftId = draftForm.value.draft_id
  const instruction = aiInstruction.value.trim()
  if (revisionRequest?.draftId !== draftId || revisionRequest?.version !== draftForm.value.version
    || revisionRequest?.instruction !== instruction) {
    revisionRequest = { draftId, version: draftForm.value.version, instruction, clientId: crypto.randomUUID() }
  }
  revisionAbort = new AbortController()
  try {
    const task = await requestDraftRevision(draftId, {
      expected_version: revisionRequest.version, instruction,
      client_request_id: revisionRequest.clientId,
    })
    if (await waitForDraftRevision(task.run_id, draftId, revisionAbort.signal)) {
      const saved = await getMailDraft(draftId)
      draftForm.value = { ...saved, toText: (saved.to ?? []).join(', '), ccText: (saved.cc ?? []).join(', ') }
      savedDraftSnapshot = JSON.stringify(draftPayload())
      revisionRequest = null
      aiInstruction.value = ''
      await loadDrafts()
    }
  } catch (cause) { draftError.value = errorDetail(cause)?.message || cause.message }
  finally { aiRevising.value = false; revisionAbort = null }
}
const confirmingDraftId = ref(null)
const sendConfirmationError = ref('')
const extractionsByEmail = ref({})
const selectedTodoIndexesByEmail = ref({})
const mailOperations = ref({})
const mailRunEventNos = ref({})
const replyInstructions = ref({})
const todoSuccessByEmail = ref({})
const streamControllers = new Map()
const mailDetailVisible = ref(false)
const selectedMail = ref(null)
const mailDetail = ref(null)
const mailDetailLoading = ref(false)
const mailDetailError = ref('')

const receivedCount = computed(() => messages.value.length)
const draftCount = computed(() => drafts.value.length)
const sentCount = computed(() => sentMessages.value.length)

function formatTime(value) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

function mailTimeParts(value) {
  const date = value ? new Date(value) : null
  if (!date || Number.isNaN(date.getTime())) return { date: '—', time: '' }
  return {
    date: new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(date),
    time: new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(date),
  }
}

function extractionStatus(status) {
  return {
    not_generated: '尚未生成',
    running: '正在生成',
    completed: '已生成',
    stale: '邮件已更新，需重新生成',
    failed: '生成失败',
  }[status] ?? status
}

function draftStatus(status) {
  return {
    draft: '可编辑',
    pending_approval: '待发送确认',
    sent: '已发送',
    failed: '发送失败',
  }[status] ?? status
}

function sentStatus(status) {
  return {
    sent: '已发送',
    failed: '发送失败',
    unknown: '发送状态未知',
  }[status] ?? status
}

function recipientsText(recipients = {}) {
  const to = Array.isArray(recipients.to) ? recipients.to : []
  const cc = Array.isArray(recipients.cc) ? recipients.cc : []
  return [...to, ...cc].join('、') || '—'
}

function splitRecipients(value) {
  return value.split(/[,\n，]/).map((item) => item.trim()).filter(Boolean)
}

function extractionFor(emailId) {
  return extractionsByEmail.value[emailId] ?? null
}

function selectedTodoIndexes(emailId) {
  return selectedTodoIndexesByEmail.value[emailId] ?? []
}

function operationFor(emailId) {
  return mailOperations.value[emailId] ?? null
}

function setOperation(emailId, operation) {
  mailOperations.value[emailId] = operation
}

function applyExtraction(emailId, extraction) {
  extractionsByEmail.value[emailId] = {
    extraction_id: extraction.extraction_id,
    key_points: extraction.key_points ?? [],
    todos: extraction.todos ?? [],
    due_date_candidates: extraction.due_date_candidates ?? [],
  }
  const message = messages.value.find((item) => item.email_id === emailId)
  if (message) message.extraction_status = 'completed'
}

function toggleTodo(emailId, index, checked) {
  const selected = new Set(selectedTodoIndexes(emailId))
  if (checked) selected.add(index)
  else selected.delete(index)
  selectedTodoIndexesByEmail.value[emailId] = [...selected]
}

function addressList(addresses = []) {
  return addresses.map((address) => address.name
    ? address.name + ' <' + address.email + '>'
    : address.email).join('、') || '—'
}

async function openMailDetail(message) {
  selectedMail.value = message
  mailDetailVisible.value = true
  mailDetail.value = null
  mailDetailError.value = ''
  mailDetailLoading.value = true
  try {
    mailDetail.value = await getMailMessageDetail(message.email_id)
  } catch (cause) {
    mailDetailError.value = errorDetail(cause)?.message || cause.message || '加载邮件详情失败。'
  } finally {
    mailDetailLoading.value = false
  }
}

function errorDetail(cause) {
  const detail = cause.response?.data?.detail
  return typeof detail === 'object' ? detail : null
}

async function loadMessages() {
  loading.value = true
  error.value = ''
  try {
    const result = await listMailMessages()
    messages.value = result.items ?? []
    snapshotAt.value = result.snapshot_at
    mailState.value = 'connected'
  } catch (cause) {
    const detail = errorDetail(cause)
    messages.value = []
    if (cause.response?.status === 409 && detail?.code === 'CONNECTION_REQUIRED') {
      mailState.value = 'connection_required'
      error.value = detail.message || '尚未连接具有邮件读取权限的工作邮箱。'
    } else if (cause.response?.status === 503 && detail?.code === 'MAIL_TOOL_UNAVAILABLE') {
      mailState.value = 'unavailable'
      error.value = detail.message || '邮件工具服务暂时不可用。'
    } else {
      mailState.value = 'error'
      error.value = detail?.message || cause.message || '加载邮件失败。'
    }
  } finally {
    loading.value = false
  }
}

async function toggleImportance(message) {
  flagBusyId.value = message.email_id
  flagError.value = ''
  try {
    const result = await setMailImportance(message.email_id, !message.is_important)
    messages.value = messages.value.map((item) => item.email_id === message.email_id
      ? { ...item, is_important: result.is_important } : item)
    ElMessage.success(result.is_important ? '已标为重要邮件。' : '已取消重要标记。')
  } catch (cause) {
    flagError.value = errorDetail(cause)?.message || cause.message || '保存重要标记失败。'
  } finally {
    flagBusyId.value = null
  }
}

async function requestExtraction(message) {
  const emailId = message.email_id
  setOperation(emailId, { type: 'extraction', status: 'submitting', error: '' })
  try {
    const result = await requestMailExtraction(emailId)
    if (result.cached && result.extraction) {
      applyExtraction(emailId, { extraction_id: result.extraction_id, ...result.extraction })
      setOperation(emailId, { type: 'extraction', status: 'completed', error: '' })
      return
    }
    setOperation(emailId, { type: 'extraction', status: 'running', runId: result.run_id, error: '' })
    if (!result.run_id) throw new Error('摘要任务未返回运行标识。')
    void streamMailRun(result.run_id, emailId, 'extraction')
  } catch (cause) {
    setOperation(emailId, { type: 'extraction', status: 'failed', error: errorDetail(cause)?.message || cause.message || '提交摘要任务失败。' })
  }
}

async function requestReplyDraft(message) {
  const emailId = message.email_id
  setOperation(emailId, { type: 'reply', status: 'submitting', error: '' })
  try {
    const extraction = extractionFor(emailId)
    const result = await requestMailReplyDraft(emailId, {
      ...(extraction?.extraction_id ? { extraction_id: extraction.extraction_id } : {}),
      ...(replyInstructions.value[emailId]?.trim() ? { instruction: replyInstructions.value[emailId].trim() } : {}),
    })
    setOperation(emailId, { type: 'reply', status: 'running', runId: result.run_id, error: '' })
    if (!result.run_id) throw new Error('回复草稿任务未返回运行标识。')
    void streamMailRun(result.run_id, emailId, 'reply')
  } catch (cause) {
    setOperation(emailId, { type: 'reply', status: 'failed', error: errorDetail(cause)?.message || cause.message || '提交回复草稿任务失败。' })
  }
}

async function createTodoDrafts(message) {
  const emailId = message.email_id
  const extraction = extractionFor(emailId)
  const todoIndexes = selectedTodoIndexes(emailId)
  if (!extraction?.extraction_id || !todoIndexes.length) return
  setOperation(emailId, { type: 'todo', status: 'submitting', error: '' })
  try {
    const drafts = await createMailTodoDrafts(emailId, {
      extraction_id: extraction.extraction_id,
      todo_indexes: todoIndexes,
    })
    selectedTodoIndexesByEmail.value[emailId] = []
    todoSuccessByEmail.value[emailId] = '已加入 ' + drafts.length + ' 项待办草稿。'
    setOperation(emailId, { type: 'todo', status: 'completed', error: '' })
  } catch (cause) {
    setOperation(emailId, { type: 'todo', status: 'failed', error: errorDetail(cause)?.message || cause.message || '创建待办草稿失败。' })
  }
}

async function streamMailRun(runId, emailId, kind) {
  streamControllers.get(runId)?.abort()
  const controller = new AbortController()
  streamControllers.set(runId, controller)
  try {
    await subscribeRunEvents(runId, {
      lastEventId: mailRunEventNos.value[runId] ?? 0,
      signal: controller.signal,
      onEvent: async (event) => {
        const payload = event.data?.payload ?? {}
        const eventNo = Number(event.data?.event_no ?? event.id)
        if (Number.isSafeInteger(eventNo)) mailRunEventNos.value[runId] = eventNo

        if (event.event === 'mail_extraction_completed') {
          applyExtraction(emailId, payload)
          setOperation(emailId, { type: kind, status: 'completed', runId, error: '' })
        }
        if (event.event === 'mail_reply_draft_completed') {
          draftsLoaded.value = false
          await loadDrafts()
          setOperation(emailId, { type: kind, status: 'completed', runId, error: '' })
          controller.abort()
        }
        if (event.event === 'input_required') {
          setOperation(emailId, { type: kind, status: 'waiting_input', runId, error: '' })
          controller.abort()
        }
        if (event.event === 'run_failed') {
          setOperation(emailId, {
            type: kind,
            status: 'failed',
            runId,
            error: payload.error_message ?? payload.message ?? '邮件任务执行失败。',
          })
          controller.abort()
        }
        if (event.event === 'run_cancelled') {
          setOperation(emailId, { type: kind, status: 'cancelled', runId, error: '' })
          controller.abort()
        }
      },
    })
  } catch (cause) {
    if (!controller.signal.aborted) {
      startBackgroundPoll(runId)
    }
  } finally {
    if (streamControllers.get(runId) === controller) {
      streamControllers.delete(runId)
      if (!controller.signal.aborted && ['running', 'submitting'].includes(operationFor(emailId)?.status)) startBackgroundPoll(runId)
    }
  }
}

async function loadDrafts() {
  loading.value = true
  draftsError.value = ''
  try {
    drafts.value = await listMailDrafts()
    draftsLoaded.value = true
  } catch (cause) {
    draftsError.value = errorDetail(cause)?.message || cause.message || '加载草稿失败。'
  } finally {
    loading.value = false
  }
}

async function loadSentMessages() {
  loading.value = true
  sentError.value = ''
  try {
    sentMessages.value = await listSentMailMessages()
    sentLoaded.value = true
  } catch (cause) {
    sentError.value = errorDetail(cause)?.message || cause.message || '加载已发送邮件失败。'
  } finally {
    loading.value = false
  }
}

async function toggleFollowUp(message) {
  flagBusyId.value = message.sent_message_id
  flagError.value = ''
  try {
    const result = await setSentMailFollowUp(message.sent_message_id, !message.awaiting_reply)
    sentMessages.value = sentMessages.value.map((item) => item.sent_message_id === message.sent_message_id
      ? { ...item, awaiting_reply: result.awaiting_reply } : item)
    ElMessage.success(result.awaiting_reply ? '已加入等待回复事项。' : '已结束等待回复。')
  } catch (cause) {
    flagError.value = errorDetail(cause)?.message || cause.message || '保存等待回复状态失败。'
  } finally {
    flagBusyId.value = null
  }
}

async function selectTab(tab) {
  activeTab.value = tab
  if (tab === 'drafts' && !draftsLoaded.value) await loadDrafts()
  if (tab === 'sent' && !sentLoaded.value) await loadSentMessages()
}

async function refreshCurrentTab() {
  if (activeTab.value === 'received') await loadMessages()
  if (activeTab.value === 'drafts') await loadDrafts()
  if (activeTab.value === 'sent') await loadSentMessages()
}

async function openDraft(draftId) {
  aiInstruction.value = ''
  savedDraftSnapshot = ''
  revisionRequest = null
  draftDetailLoading.value = true
  draftError.value = ''
  draftDialogVisible.value = true
  draftForm.value = null
  try {
    const draft = await getMailDraft(draftId)
    draftForm.value = {
      ...draft,
      toText: (draft.to ?? []).join(', '),
      ccText: (draft.cc ?? []).join(', '),
    }
    savedDraftSnapshot = JSON.stringify(draftPayload())
  } catch (cause) {
    draftError.value = errorDetail(cause)?.message || cause.message || '加载草稿详情失败。'
  } finally {
    draftDetailLoading.value = false
  }
}

function draftPayload() {
  return {
    to: splitRecipients(draftForm.value.toText),
    cc: splitRecipients(draftForm.value.ccText),
    subject: draftForm.value.subject,
    body: draftForm.value.body,
  }
}

async function saveDraft() {
  if (!draftForm.value || draftForm.value.status !== 'draft') return false
  const payload = draftPayload()
  if (JSON.stringify(payload) === savedDraftSnapshot) return true
  draftSaving.value = true
  draftError.value = ''
  try {
    const saved = await updateMailDraft(draftForm.value.draft_id, payload)
    draftForm.value = {
      ...saved,
      toText: (saved.to ?? []).join(', '),
      ccText: (saved.cc ?? []).join(', '),
    }
    savedDraftSnapshot = JSON.stringify(draftPayload())
    revisionRequest = null
    const index = drafts.value.findIndex((item) => item.draft_id === saved.draft_id)
    if (index >= 0) drafts.value[index] = saved
    await loadDrafts()
    return true
  } catch (cause) {
    draftError.value = errorDetail(cause)?.message || cause.message || '保存草稿失败。'
    return false
  } finally {
    draftSaving.value = false
  }
}

async function submitSendConfirmation(draft) {
  confirmingDraftId.value = draft.draft_id
  sendConfirmationError.value = ''
  try {
    const result = await requestMailSendConfirmation(draft.draft_id)
    await loadDrafts()
    await router.push({
      name: 'confirmations',
      query: { run_id: result.run_id },
    })
  } catch (cause) {
    sendConfirmationError.value = errorDetail(cause)?.message || cause.message || '创建邮件发送确认失败。'
  } finally {
    confirmingDraftId.value = null
  }
}

async function signOut() {
  await auth.signOut()
  await router.replace({ name: 'login' })
}

onMounted(async () => {
  // 标签上的数量应当反映真实服务端数据，而不是等用户第一次切换分区后才更新。
  await Promise.all([loadMessages(), loadDrafts(), loadSentMessages()])
  if (typeof route.query.draft_id === 'string' && route.query.draft_id) {
    activeTab.value = 'drafts'
    await openDraft(route.query.draft_id)
  } else if (typeof route.query.email_id === 'string' && route.query.email_id) {
    activeTab.value = 'received'
    const message = messages.value.find((item) => item.email_id === route.query.email_id)
      ?? { email_id: route.query.email_id, subject: '邮件详情', from_email: '', extraction_status: 'not_generated' }
    await openMailDetail(message)
  } else if (route.query.tab === 'sent') {
    activeTab.value = 'sent'
  }
})
onBeforeUnmount(() => {
  revisionAbort?.abort()
  for (const timer of resumedPolls.values()) window.clearInterval(timer)
  for (const controller of streamControllers.values()) controller.abort()
  streamControllers.clear()
})
</script>

<template>
  <main class="workspace">
    <aside class="sidebar">
      <div class="brand"><span class="mark">⌾</span>Aegis PA</div>
      <p class="nav-label app-nav-label">工作空间</p>
      <router-link class="nav" :to="{ name: 'today' }">▦ 今日工作台</router-link>
      <router-link class="nav" :to="{ name: 'tasks' }">◌ 任务对话</router-link>
      <router-link class="nav" :to="{ name: 'calendar' }">▦ 日历</router-link>
      <router-link class="nav active" :to="{ name: 'mail-management' }">✉ 邮件管理</router-link>
      <router-link class="nav" :to="{ name: 'todo-plans' }">☷ 待办计划</router-link>
      <router-link class="nav" :to="{ name: 'confirmations' }">✓ 操作确认</router-link>
      <router-link class="nav" :to="{ name: 'knowledge' }">▤ 知识资料</router-link>
      <router-link class="nav" :to="{ name: 'memories' }">◇ 长期记忆</router-link>
      <span class="nav disabled">◫ 连接与审计（待接入）</span>

      <div class="identity">
        <b>{{ auth.profile?.name }}</b>
        <span>{{ auth.profile?.email || '已通过 Keycloak 登录' }}</span>
        <el-button text type="primary" @click="signOut">退出登录</el-button>
      </div>
    </aside>

    <section class="content mail-content">
      <header class="page-header">
        <div>
          <h1>邮件管理</h1>
          <p class="muted">统一查看收到邮件、保存草稿和已发送邮件。草稿不会自动发送，发送前仍需逐项确认。</p>
        </div>
        <span class="badge" :class="mailState === 'connected' ? 'success' : 'neutral'">
          {{ mailState === 'connected' ? '工作邮箱已连接' : '邮箱状态待确认' }}
        </span>
      </header>

      <article v-loading="loading" class="card mail-card">
        <div class="card-head">
          <div>
            <h2>工作邮箱</h2>
            <p class="muted">最近同步：{{ snapshotAt ? formatTime(snapshotAt) : '尚未同步' }}</p>
          </div>
          <el-button :loading="loading" @click="refreshCurrentTab">刷新邮件</el-button>
        </div>

        <div class="mail-card-body">
          <RunInputPanel @answered="resumeBackground" @cancelled="cancelBackground" />
          <el-alert v-if="flagError" :title="flagError" type="error" :closable="false" show-icon />
          <div class="mail-tabs" role="tablist" aria-label="邮件分区">
            <button class="mail-tab" :class="{ active: activeTab === 'received' }" type="button" @click="selectTab('received')">
              收到 <span>{{ receivedCount }}</span>
            </button>
            <button class="mail-tab" :class="{ active: activeTab === 'drafts' }" type="button" @click="selectTab('drafts')">
              保存草稿 <span>{{ draftCount }}</span>
            </button>
            <button class="mail-tab" :class="{ active: activeTab === 'sent' }" type="button" @click="selectTab('sent')">
              已发送 <span>{{ sentCount }}</span>
            </button>
          </div>

          <section v-if="activeTab === 'received'" class="mail-panel">
            <el-alert
              v-if="mailState === 'connection_required'"
              :title="error"
              description="连接工作邮箱的页面尚未接入。连接后可刷新此列表。"
              type="warning"
              :closable="false"
              show-icon
            />
            <el-alert
              v-else-if="mailState === 'unavailable' || mailState === 'error'"
              :title="error"
              description="请稍后重试；页面不会显示未经服务端确认的邮件。"
              type="error"
              :closable="false"
              show-icon
            />
            <template v-else>
              <div class="mail-notice">
                <b>外部内容提示：</b>
                收到邮件的正文属于不可信内容。当前页面仅展示服务端返回的邮件元数据，不展示正文。
              </div>
              <div class="section-head">
                <div>
                  <h2>收到邮件</h2>
                  <p class="muted">可对单封邮件生成摘要、选择待办候选，或起草一封不会自动发送的回复。</p>
                </div>
              </div>
              <div class="mail-table-wrap">
                <table class="mail-table received-mail-table">
                  <thead><tr><th>客户与邮件</th><th>关键信息</th><th>待办与截止时间</th><th>操作</th></tr></thead>
                  <tbody>
                    <tr v-for="message in messages" :key="message.email_id">
                      <td>
                        <div class="mail-sender">{{ message.customer || message.from_email }} <span v-if="message.is_important" class="badge write">重要</span></div>
                        <div class="title">{{ message.subject }}</div>
                        <div class="detail">{{ message.from_email }} · {{ formatTime(message.received_at) }}</div>
                      </td>
                      <td class="mail-insights">
                        <template v-if="extractionFor(message.email_id)">
                          <p v-for="point in extractionFor(message.email_id).key_points" :key="point" class="mail-point">{{ point }}</p>
                          <p v-if="!extractionFor(message.email_id).key_points.length" class="muted">未提取到关键信息。</p>
                        </template>
                        <span v-else class="mail-extraction-status" :class="{ ready: message.extraction_status === 'completed' }">{{ extractionStatus(message.extraction_status) }}</span>
                        <div class="reply-compose">
                          <el-button
                            size="small"
                            :loading="operationFor(message.email_id)?.type === 'reply' && ['submitting', 'running'].includes(operationFor(message.email_id)?.status)"
                            :disabled="['submitting', 'running', 'waiting_input'].includes(operationFor(message.email_id)?.status)"
                            @click="requestReplyDraft(message)"
                          >起草回复</el-button>
                          <el-input
                            v-model="replyInstructions[message.email_id]"
                            class="reply-instruction"
                            size="small"
                            placeholder="可选：补充回复要求"
                          />
                        </div>
                      </td>
                      <td class="mail-todos">
                        <template v-if="extractionFor(message.email_id)?.todos.length">
                          <label v-for="(todo, index) in extractionFor(message.email_id).todos" :key="index" class="mail-todo-option">
                            <input
                              type="checkbox"
                              :checked="selectedTodoIndexes(message.email_id).includes(index)"
                              @change="toggleTodo(message.email_id, index, $event.target.checked)"
                            >
                            <span>{{ todo.content }}<small v-if="todo.due_at"> · 截止 {{ formatTime(todo.due_at) }}</small><small v-else-if="todo.due_text"> · {{ todo.due_text }}</small><small v-if="todo.is_inferred">（推断，需核对）</small></span>
                          </label>
                          <el-button
                            size="small"
                            :loading="operationFor(message.email_id)?.type === 'todo' && operationFor(message.email_id)?.status === 'submitting'"
                            :disabled="!selectedTodoIndexes(message.email_id).length"
                            @click="createTodoDrafts(message)"
                          >加入待办草稿</el-button>
                          <p v-if="todoSuccessByEmail[message.email_id]" class="mail-success">{{ todoSuccessByEmail[message.email_id] }}</p>
                        </template>
                        <p v-else class="muted">生成摘要后将在此显示待办候选。</p>
                      </td>
                      <td class="mail-actions">
                        <div class="received-mail-actions">
                          <el-button
                            size="small"
                            type="primary"
                            :loading="operationFor(message.email_id)?.type === 'extraction' && ['submitting', 'running'].includes(operationFor(message.email_id)?.status)"
                            :disabled="['submitting', 'running', 'waiting_input'].includes(operationFor(message.email_id)?.status)"
                            @click="requestExtraction(message)"
                          >{{ extractionFor(message.email_id) ? '重新生成摘要' : '生成摘要' }}</el-button>
                          <el-button class="mail-detail-button" size="small" plain @click="openMailDetail(message)">邮件详情</el-button>
                          <el-button size="small" plain :type="message.is_important ? 'default' : 'warning'" :loading="flagBusyId === message.email_id" @click="toggleImportance(message)">{{ message.is_important ? '取消重要' : '标为重要' }}</el-button>
                        </div>
                        <p v-if="operationFor(message.email_id)?.status === 'running'" class="mail-progress">任务已提交，正在处理…</p>
                        <p v-if="operationFor(message.email_id)?.status === 'waiting_input'" class="mail-progress">任务等待补充信息，请填写上方问题。</p>
                        <p v-if="operationFor(message.email_id)?.status === 'failed'" class="mail-error">{{ operationFor(message.email_id)?.error }}</p>
                      </td>
                    </tr>
                    <tr v-if="!loading && messages.length === 0">
                      <td colspan="4" class="mail-empty">当前邮箱没有可展示的邮件元数据。</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </template>
          </section>

          <section v-else-if="activeTab === 'drafts'" class="mail-panel">
            <el-alert
              v-if="draftsError"
              :title="draftsError"
              description="请稍后重试加载保存草稿。"
              type="error"
              :closable="false"
              show-icon
            />
            <template v-else>
              <el-alert
                v-if="sendConfirmationError"
                class="mail-send-confirmation-error"
                :title="sendConfirmationError"
                description="请检查草稿状态和邮箱发送权限后重试。"
                type="error"
                :closable="false"
                show-icon
              />
              <div class="section-head">
                <div>
                  <h2>保存草稿</h2>
                  <p class="muted">列表不展示正文；选择一项后才会加载完整内容。保存不会发送邮件。</p>
                </div>
              </div>
              <div class="mail-table-wrap">
                <table class="mail-table draft-mail-table">
                  <thead><tr><th>草稿</th><th>收件人</th><th>更新时间</th><th>状态</th><th>操作</th></tr></thead>
                  <tbody>
                    <tr v-for="draft in drafts" :key="draft.draft_id">
                      <td>
                        <div class="title">{{ draft.subject }}</div>
                        <span class="draft-version-label">版本 {{ draft.version }}</span>
                        <details v-if="draft.source_email_id" class="draft-mail-source">
                          <summary>来源邮件</summary>
                          <div>邮件 ID：{{ draft.source_email_id }}</div>
                        </details>
                      </td>
                      <td class="draft-mail-recipients">{{ (draft.to ?? []).join('、') || '—' }}</td>
                      <td class="draft-mail-time">
                        <time :datetime="draft.updated_at">
                          <span>{{ mailTimeParts(draft.updated_at).date }}</span>
                          <small>{{ mailTimeParts(draft.updated_at).time }}</small>
                        </time>
                      </td>
                      <td><span class="badge draft-mail-status" :class="draft.status === 'draft' ? 'read' : draft.status === 'pending_approval' ? 'write' : draft.status === 'sent' ? 'success' : draft.status === 'failed' ? 'failed' : 'neutral'">{{ draftStatus(draft.status) }}</span></td>
                      <td class="mail-actions">
                        <div class="draft-mail-actions">
                          <el-button size="small" plain @click="openDraft(draft.draft_id)">{{ draft.status === 'draft' ? '查看 / 编辑' : '查看草稿' }}</el-button>
                          <el-button
                            v-if="draft.status === 'draft'"
                            size="small"
                            type="primary"
                            :loading="confirmingDraftId === draft.draft_id"
                            title="创建确认项后将在确认页逐项核对并决定是否发送"
                            @click="submitSendConfirmation(draft)"
                          >提交发送确认</el-button>
                        </div>
                      </td>
                    </tr>
                    <tr v-if="!loading && draftsLoaded && drafts.length === 0">
                      <td colspan="5" class="mail-empty">尚无保存的邮件草稿。</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </template>
          </section>

          <section v-else class="mail-panel">
            <el-alert
              v-if="sentError"
              :title="sentError"
              description="请稍后重试加载已发送邮件。"
              type="error"
              :closable="false"
              show-icon
            />
            <template v-else>
              <div class="section-head">
                <div>
                  <h2>已发送</h2>
                  <p class="muted">查看已发送结果，并手动标记需要等待对方回复的邮件；收到回复后请手动结束等待。</p>
                </div>
              </div>
              <div class="mail-table-wrap">
                <table class="mail-table sent-mail-table">
                  <thead><tr><th>邮件</th><th>收件人</th><th>发送时间</th><th>状态</th><th>跟进</th></tr></thead>
                  <tbody>
                    <tr v-for="message in sentMessages" :key="message.sent_message_id">
                      <td>
                        <div class="title">{{ message.subject }}</div>
                        <details class="sent-mail-technical">
                          <summary>发送记录</summary>
                          <div>任务 ID：{{ message.run_id || '—' }}</div>
                          <div>服务商消息 ID：{{ message.provider_message_id || '—' }}</div>
                        </details>
                      </td>
                      <td class="sent-mail-recipients">{{ recipientsText(message.recipients) }}</td>
                      <td class="sent-mail-time">
                        <time :datetime="message.sent_at">
                          <span>{{ mailTimeParts(message.sent_at).date }}</span>
                          <small>{{ mailTimeParts(message.sent_at).time }}</small>
                        </time>
                      </td>
                      <td><span class="badge sent-mail-status" :class="message.status === 'sent' ? 'success' : 'neutral'">{{ sentStatus(message.status) }}</span></td>
                      <td class="mail-actions">
                        <div class="sent-mail-follow-up">
                          <el-button size="small" plain :type="message.awaiting_reply ? 'default' : 'primary'" :disabled="message.status !== 'sent'" :loading="flagBusyId === message.sent_message_id" @click="toggleFollowUp(message)">{{ message.awaiting_reply ? '结束等待' : '标记等待回复' }}</el-button>
                          <span v-if="message.awaiting_reply" class="badge write">等待回复</span>
                        </div>
                      </td>
                    </tr>
                    <tr v-if="!loading && sentLoaded && sentMessages.length === 0">
                      <td colspan="5" class="mail-empty">尚无已发送邮件记录。</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </template>
          </section>
        </div>
      </article>
    </section>

    <el-dialog v-model="draftDialogVisible" class="mail-draft-dialog" title="邮件草稿" width="min(720px, calc(100vw - 32px))" :close-on-click-modal="!aiRevising" :close-on-press-escape="!aiRevising" :show-close="!aiRevising" destroy-on-close>
      <div v-loading="draftDetailLoading">
        <el-alert v-if="draftError" :title="draftError" type="error" :closable="false" show-icon />
        <template v-else-if="draftForm">
          <p class="draft-version">草稿 v{{ draftForm.version }} · {{ draftStatus(draftForm.status) }}</p>
          <p v-if="aiRevising" class="muted">正在按要求修改正文，完成后会重新读取草稿供你核对；不会自动发送。</p>
          <el-form label-position="top">
            <el-form-item label="收件人">
              <el-input v-model="draftForm.toText" :disabled="draftForm.status !== 'draft' || aiRevising || draftSaving" placeholder="多个地址以逗号分隔" />
            </el-form-item>
            <el-form-item label="抄送">
              <el-input v-model="draftForm.ccText" :disabled="draftForm.status !== 'draft' || aiRevising || draftSaving" placeholder="多个地址以逗号分隔，可留空" />
            </el-form-item>
            <el-form-item label="主题">
              <el-input v-model="draftForm.subject" :disabled="draftForm.status !== 'draft' || aiRevising || draftSaving" />
            </el-form-item>
            <el-form-item label="正文">
              <el-input v-model="draftForm.body" type="textarea" :rows="10" :disabled="draftForm.status !== 'draft' || aiRevising || draftSaving" />
            </el-form-item>
            <el-form-item v-if="draftForm.status === 'draft'" label="AI 修改要求（仅修改正文）">
              <el-input v-model="aiInstruction" type="textarea" :rows="2" maxlength="2000" :disabled="aiRevising" placeholder="例如：语气正式一点，保留原有事实" />
            </el-form-item>
          </el-form>
          <p v-if="draftForm.status !== 'draft'" class="muted">此草稿当前状态不允许编辑。</p>
        </template>
      </div>
      <template #footer>
        <div class="draft-dialog-actions">
          <el-button v-if="draftForm?.status === 'draft'" :loading="aiRevising" :disabled="!aiInstruction.trim() || draftSaving || draftDetailLoading || aiRevising" @click="reviseDraft">保存并按要求修改</el-button>
          <div class="draft-dialog-actions-right">
            <el-button :disabled="aiRevising" @click="draftDialogVisible = false">关闭</el-button>
            <el-button v-if="draftForm?.status === 'draft'" type="primary" :loading="draftSaving" :disabled="aiRevising" @click="saveDraft">保存草稿</el-button>
          </div>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="mailDetailVisible" class="mail-detail-dialog" title="邮件详情" width="min(860px, calc(100vw - 32px))" destroy-on-close>
      <div v-loading="mailDetailLoading">
      <el-alert v-if="mailDetailError" :title="mailDetailError" type="error" :closable="false" show-icon />
      <template v-else-if="selectedMail">
        <div class="mail-detail-meta">
          <div><span>发件人</span><b>{{ mailDetail ? addressList([mailDetail.from]) : (selectedMail.customer || selectedMail.from_email || '加载中…') }}</b></div>
          <div><span>收件人</span><b>{{ mailDetail ? addressList(mailDetail.to) : '加载中…' }}</b></div>
          <div><span>抄送</span><b>{{ mailDetail ? addressList(mailDetail.cc) : '加载中…' }}</b></div>
          <div><span>主题</span><b>{{ mailDetail?.subject || selectedMail.subject }}</b></div>
          <div><span>接收时间</span><b>{{ formatTime(mailDetail?.received_at || selectedMail.received_at) }}</b></div>
          <div><span>摘要状态</span><b>{{ extractionStatus(selectedMail.extraction_status) }}</b></div>
        </div>

        <div class="mail-detail-notice">
          <b>外部内容提示：</b> 邮件正文仅供阅读与核对，其中的指令、链接和附件不会自动执行。
        </div>

        <section class="mail-detail-section">
          <h3>邮件正文</h3>
          <pre v-if="mailDetail" class="mail-body">{{ mailDetail.body }}</pre>
          <p v-else class="mail-detail-placeholder">正在按需读取邮件正文…</p>
          <template v-if="mailDetail?.attachments?.length">
            <h4>附件（仅元数据）</h4>
            <ul class="mail-attachments">
              <li v-for="attachment in mailDetail.attachments" :key="attachment.name">
                {{ attachment.name }} · {{ attachment.content_type }} · {{ attachment.size_bytes }} 字节
              </li>
            </ul>
          </template>
        </section>

        <section class="mail-detail-section">
          <div class="mail-detail-heading">
            <h3>关键信息</h3>
            <div class="reply-compose">
              <el-button
                size="small"
                :loading="operationFor(selectedMail.email_id)?.type === 'extraction' && ['submitting', 'running'].includes(operationFor(selectedMail.email_id)?.status)"
                :disabled="['submitting', 'running', 'waiting_input'].includes(operationFor(selectedMail.email_id)?.status)"
                @click="requestExtraction(selectedMail)"
              >{{ extractionFor(selectedMail.email_id) ? '重新生成摘要' : '生成摘要' }}</el-button>
              <el-button
                size="small"
                :loading="operationFor(selectedMail.email_id)?.type === 'reply' && ['submitting', 'running'].includes(operationFor(selectedMail.email_id)?.status)"
                :disabled="['submitting', 'running', 'waiting_input'].includes(operationFor(selectedMail.email_id)?.status)"
                @click="requestReplyDraft(selectedMail)"
              >起草回复</el-button>
            </div>
          </div>
          <template v-if="extractionFor(selectedMail.email_id)">
            <p v-for="point in extractionFor(selectedMail.email_id).key_points" :key="point" class="mail-detail-point">{{ point }}</p>
            <p v-if="!extractionFor(selectedMail.email_id).key_points.length" class="muted">未提取到关键信息。</p>
          </template>
          <p v-else class="mail-detail-placeholder">尚未生成摘要。点击“生成摘要”后将在这里展示关键信息、待办候选与截止时间。</p>
        </section>

        <section class="mail-detail-section">
          <h3>待办与截止时间</h3>
          <template v-if="extractionFor(selectedMail.email_id)?.todos.length">
            <label v-for="(todo, index) in extractionFor(selectedMail.email_id).todos" :key="index" class="mail-todo-option">
              <input
                type="checkbox"
                :checked="selectedTodoIndexes(selectedMail.email_id).includes(index)"
                @change="toggleTodo(selectedMail.email_id, index, $event.target.checked)"
              >
              <span>{{ todo.content }}<small v-if="todo.due_at"> · 截止 {{ formatTime(todo.due_at) }}</small><small v-else-if="todo.due_text"> · {{ todo.due_text }}</small><small v-if="todo.is_inferred">（推断，需核对）</small></span>
            </label>
            <el-button
              size="small"
              :loading="operationFor(selectedMail.email_id)?.type === 'todo' && operationFor(selectedMail.email_id)?.status === 'submitting'"
              :disabled="!selectedTodoIndexes(selectedMail.email_id).length"
              @click="createTodoDrafts(selectedMail)"
            >加入待办草稿</el-button>
          </template>
          <p v-else class="mail-detail-placeholder">生成摘要后将在这里展示待办候选。</p>
          <p v-if="todoSuccessByEmail[selectedMail.email_id]" class="mail-success">{{ todoSuccessByEmail[selectedMail.email_id] }}</p>
        </section>

        <section class="mail-detail-section">
          <h3>补充回复要求</h3>
          <el-input v-model="replyInstructions[selectedMail.email_id]" type="textarea" :rows="3" placeholder="例如：语气简洁，询问报价确认时间（可选）" />
        </section>
        <p v-if="operationFor(selectedMail.email_id)?.status === 'running'" class="mail-progress">任务已提交，正在处理…</p>
        <p v-if="operationFor(selectedMail.email_id)?.status === 'failed'" class="mail-error">{{ operationFor(selectedMail.email_id)?.error }}</p>
      </template>
      </div>
      <template #footer>
        <el-button @click="mailDetailVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </main>
</template>
