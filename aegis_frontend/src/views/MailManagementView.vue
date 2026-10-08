<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
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
} from '../api/mail'
import { useAuthStore } from '../store/auth'
import { subscribeRunEvents } from '../api/run'

const auth = useAuthStore()
const router = useRouter()
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
const draftDialogVisible = ref(false)
const draftDetailLoading = ref(false)
const draftSaving = ref(false)
const draftError = ref('')
const draftForm = ref(null)
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
        }
        if (event.event === 'run_failed') {
          setOperation(emailId, {
            type: kind,
            status: 'failed',
            runId,
            error: payload.error_message ?? payload.message ?? '邮件任务执行失败。',
          })
        }
      },
    })
  } catch (cause) {
    if (!controller.signal.aborted) {
      setOperation(emailId, { type: kind, status: 'failed', runId, error: cause.message || '实时任务连接失败。' })
    }
  } finally {
    if (streamControllers.get(runId) === controller) streamControllers.delete(runId)
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
  } catch (cause) {
    draftError.value = errorDetail(cause)?.message || cause.message || '加载草稿详情失败。'
  } finally {
    draftDetailLoading.value = false
  }
}

async function saveDraft() {
  if (!draftForm.value || draftForm.value.status !== 'draft') return
  draftSaving.value = true
  draftError.value = ''
  try {
    const saved = await updateMailDraft(draftForm.value.draft_id, {
      to: splitRecipients(draftForm.value.toText),
      cc: splitRecipients(draftForm.value.ccText),
      subject: draftForm.value.subject,
      body: draftForm.value.body,
    })
    draftForm.value = {
      ...saved,
      toText: (saved.to ?? []).join(', '),
      ccText: (saved.cc ?? []).join(', '),
    }
    const index = drafts.value.findIndex((item) => item.draft_id === saved.draft_id)
    if (index >= 0) drafts.value[index] = saved
    await loadDrafts()
  } catch (cause) {
    draftError.value = errorDetail(cause)?.message || cause.message || '保存草稿失败。'
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
})
onBeforeUnmount(() => {
  for (const controller of streamControllers.values()) controller.abort()
  streamControllers.clear()
})
</script>

<template>
  <main class="workspace">
    <aside class="sidebar">
      <div class="brand"><span class="mark">⌾</span>Aegis PA</div>
      <p class="nav-label app-nav-label">工作空间</p>
      <router-link class="nav" :to="{ name: 'tasks' }">◌ 任务对话</router-link>
      <router-link class="nav active" :to="{ name: 'mail-management' }">✉ 邮件管理</router-link>
      <router-link class="nav" :to="{ name: 'todo-plans' }">☷ 待办计划</router-link>
      <router-link class="nav" :to="{ name: 'confirmations' }">✓ 操作确认</router-link>
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
                <table class="mail-table">
                  <thead><tr><th>客户与邮件</th><th>关键信息</th><th>待办与截止时间</th><th>操作</th></tr></thead>
                  <tbody>
                    <tr v-for="message in messages" :key="message.email_id">
                      <td>
                        <div class="title">{{ message.customer || message.from_email }} · {{ message.subject }}</div>
                        <div class="detail">{{ message.from_email }} · {{ formatTime(message.received_at) }}</div>
                      </td>
                      <td class="mail-insights">
                        <template v-if="extractionFor(message.email_id)">
                          <p v-for="point in extractionFor(message.email_id).key_points" :key="point" class="mail-point">{{ point }}</p>
                          <p v-if="!extractionFor(message.email_id).key_points.length" class="muted">未提取到关键信息。</p>
                        </template>
                        <p v-else class="muted">{{ extractionStatus(message.extraction_status) }}</p>
                        <div class="reply-compose">
                          <el-button
                            size="small"
                            :loading="operationFor(message.email_id)?.type === 'reply' && ['submitting', 'running'].includes(operationFor(message.email_id)?.status)"
                            :disabled="operationFor(message.email_id)?.status === 'running'"
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
                        <el-button
                          size="small"
                          type="primary"
                          :loading="operationFor(message.email_id)?.type === 'extraction' && ['submitting', 'running'].includes(operationFor(message.email_id)?.status)"
                          :disabled="operationFor(message.email_id)?.status === 'running'"
                          @click="requestExtraction(message)"
                        >{{ extractionFor(message.email_id) ? '重新生成摘要' : '生成摘要' }}</el-button>
                        <el-button class="mail-detail-button" size="small" @click="openMailDetail(message)">邮件详情</el-button>
                        <p v-if="operationFor(message.email_id)?.status === 'running'" class="mail-progress">任务已提交，正在处理…</p>
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
                <table class="mail-table">
                  <thead><tr><th>草稿</th><th>收件人</th><th>更新时间</th><th>状态</th><th>操作</th></tr></thead>
                  <tbody>
                    <tr v-for="draft in drafts" :key="draft.draft_id">
                      <td>
                        <div class="title">{{ draft.subject }}</div>
                        <div class="detail">草稿 v{{ draft.version }} · 来源邮件 {{ draft.source_email_id }}</div>
                      </td>
                      <td>{{ (draft.to ?? []).join('、') || '—' }}</td>
                      <td>{{ formatTime(draft.updated_at) }}</td>
                      <td><span class="badge neutral">{{ draftStatus(draft.status) }}</span></td>
                      <td class="mail-actions">
                        <el-button size="small" @click="openDraft(draft.draft_id)">{{ draft.status === 'draft' ? '查看 / 编辑' : '查看草稿' }}</el-button>
                        <el-button
                          size="small"
                          type="primary"
                          :loading="confirmingDraftId === draft.draft_id"
                          :disabled="draft.status !== 'draft'"
                          :title="draft.status === 'draft' ? '创建确认项后将在确认页逐项核对并决定是否发送' : '当前草稿正在等待确认或已不可再次提交'"
                          @click="submitSendConfirmation(draft)"
                        >{{ draft.status === 'draft' ? '提交发送确认' : '等待发送确认' }}</el-button>
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
                  <p class="muted">查看服务端已记录的发送结果；尚未实现发送能力时，此列表通常为空。</p>
                </div>
              </div>
              <div class="mail-table-wrap">
                <table class="mail-table">
                  <thead><tr><th>邮件</th><th>收件人</th><th>发送时间</th><th>状态</th></tr></thead>
                  <tbody>
                    <tr v-for="message in sentMessages" :key="message.sent_message_id">
                      <td>
                        <div class="title">{{ message.subject }}</div>
                        <div class="detail">来源运行 {{ message.run_id || '—' }} · Provider {{ message.provider_message_id }}</div>
                      </td>
                      <td>{{ recipientsText(message.recipients) }}</td>
                      <td>{{ formatTime(message.sent_at) }}</td>
                      <td><span class="badge" :class="message.status === 'sent' ? 'success' : 'neutral'">{{ sentStatus(message.status) }}</span></td>
                    </tr>
                    <tr v-if="!loading && sentLoaded && sentMessages.length === 0">
                      <td colspan="4" class="mail-empty">尚无已发送邮件记录。</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </template>
          </section>
        </div>
      </article>
    </section>

    <el-dialog v-model="draftDialogVisible" title="邮件草稿" width="min(720px, calc(100vw - 32px))" destroy-on-close>
      <div v-loading="draftDetailLoading">
        <el-alert v-if="draftError" :title="draftError" type="error" :closable="false" show-icon />
        <template v-else-if="draftForm">
          <p class="draft-version">草稿 v{{ draftForm.version }} · {{ draftStatus(draftForm.status) }}</p>
          <el-form label-position="top">
            <el-form-item label="收件人">
              <el-input v-model="draftForm.toText" :disabled="draftForm.status !== 'draft'" placeholder="多个地址以逗号分隔" />
            </el-form-item>
            <el-form-item label="抄送">
              <el-input v-model="draftForm.ccText" :disabled="draftForm.status !== 'draft'" placeholder="多个地址以逗号分隔，可留空" />
            </el-form-item>
            <el-form-item label="主题">
              <el-input v-model="draftForm.subject" :disabled="draftForm.status !== 'draft'" />
            </el-form-item>
            <el-form-item label="正文">
              <el-input v-model="draftForm.body" type="textarea" :rows="10" :disabled="draftForm.status !== 'draft'" />
            </el-form-item>
          </el-form>
          <p v-if="draftForm.status !== 'draft'" class="muted">此草稿当前状态不允许编辑。</p>
        </template>
      </div>
      <template #footer>
        <el-button @click="draftDialogVisible = false">关闭</el-button>
        <el-button v-if="draftForm?.status === 'draft'" type="primary" :loading="draftSaving" @click="saveDraft">保存草稿</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="mailDetailVisible" class="mail-detail-dialog" title="邮件详情" width="min(860px, calc(100vw - 32px))" destroy-on-close>
      <div v-loading="mailDetailLoading">
      <el-alert v-if="mailDetailError" :title="mailDetailError" type="error" :closable="false" show-icon />
      <template v-else-if="selectedMail">
        <div class="mail-detail-meta">
          <div><span>发件人</span><b>{{ mailDetail ? addressList([mailDetail.from]) : (selectedMail.customer || selectedMail.from_email) + ' · ' + selectedMail.from_email }}</b></div>
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
                :disabled="operationFor(selectedMail.email_id)?.status === 'running'"
                @click="requestExtraction(selectedMail)"
              >{{ extractionFor(selectedMail.email_id) ? '重新生成摘要' : '生成摘要' }}</el-button>
              <el-button
                size="small"
                :loading="operationFor(selectedMail.email_id)?.type === 'reply' && ['submitting', 'running'].includes(operationFor(selectedMail.email_id)?.status)"
                :disabled="operationFor(selectedMail.email_id)?.status === 'running'"
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
