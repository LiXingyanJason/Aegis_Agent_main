<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { subscribeRunEvents } from '../api/run'
import { useAuthStore } from '../store/auth'
import { useConversationStore } from '../store/conversation'

const auth = useAuthStore()
const conversations = useConversationStore()
const router = useRouter()
const route = useRoute()
const draftMessage = ref('')
const conversationTitle = ref('')
const pendingClientMessageId = ref(null)
const error = ref('')
const isPolling = ref(false)
const isRunActive = ref(false)
const messagesElement = ref(null)
let pollTimer = null
let pollingRunId = null
let pollingInFlight = false
let streamAbortController = null
let streamingRunId = null
let streamRetryTimer = null

const activeConversation = computed(() => conversations.current)

function formatTime(value) {
  if (!value) return '暂无消息'
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

function formatCalendarData(value) {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.join('、')
  if (typeof value !== 'object') return String(value)
  return Object.entries(value)
    .map(([key, item]) => key + '：' + formatCalendarData(item))
    .join('；')
}

function formatDateTime(value) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'Asia/Shanghai',
  }).format(new Date(value))
}

function meetingPreview(run) {
  return run.plan_previews?.[0]?.preview
    ?? run.approval_items?.find((item) => item.action === 'calendar.create_event')?.preview_snapshot
    ?? run.approval_required?.preview
    ?? null
}

function meetingApproval(run) {
  return run.approval_items?.find((item) => item.action === 'calendar.create_event')
    ?? run.approval_required
    ?? null
}

function canConfirmMeeting(run) {
  return meetingApproval(run)?.status === 'pending'
}

function meetingPlanHeading(run) {
  const status = meetingApproval(run)?.status
  if (status === 'rejected') return '会议计划已拒绝'
  if (status === 'approved_executing') return '正在创建日历事件'
  if (status === 'executed') return '日历事件已创建'
  if (status === 'failed') return '日历事件创建失败'
  return '待执行会议计划'
}

function meetingPlanOutcome(run) {
  const approval = meetingApproval(run)
  if (approval?.status === 'rejected') return '你已拒绝此会议计划，日历不会发生变更。'
  if (approval?.status === 'approved_executing') return '已批准，正在创建日历事件。'
  if (approval?.status === 'executed') return approval.provider_resource_id
    ? '日历事件已创建，事件 ID：' + approval.provider_resource_id
    : '日历事件已创建。'
  if (approval?.status === 'failed') return '日历事件创建失败，请查看执行进度中的错误信息。'
  return ''
}

function meetingRunForMessage(message) {
  if (!message.run_id) return null
  return conversations.runs.find((run) => run.run_id === message.run_id) ?? null
}

function isTrackableRun(run) {
  return !['completed', 'failed', 'cancelled', 'waiting_confirmation', 'waiting_approval']
    .includes(run.status)
}

async function scrollMessagesToBottom() {
  await nextTick()
  const element = messagesElement.value
  if (element) element.scrollTop = element.scrollHeight
}

async function loadHistory() {
  error.value = ''
  try {
    await conversations.loadHistory()
  } catch (cause) {
    error.value = cause.response?.data?.detail || cause.message || '加载历史会话失败。'
  }
}

async function restoreConversation(conversationId, syncUrl = true) {
  stopRunTracking()
  error.value = ''
  try {
    const detail = await conversations.loadConversation(conversationId)
    await scrollMessagesToBottom()
    draftMessage.value = ''
    pendingClientMessageId.value = null
    const activeRun = detail.runs?.find(isTrackableRun)
    if (activeRun) {
      isRunActive.value = true
      startRunPolling(activeRun.run_id, conversationId)
    }
    if (syncUrl) {
      await router.replace({ query: { conversation_id: conversationId } })
    }
  } catch (cause) {
    error.value = cause.response?.data?.detail || cause.message || '恢复会话失败。'
  }
}

async function startNewConversation() {
  stopRunTracking()
  error.value = ''
  try {
    await conversations.start(conversationTitle.value.trim())
    conversationTitle.value = ''
    draftMessage.value = ''
    pendingClientMessageId.value = null
    await router.replace({ query: { conversation_id: conversations.current.conversation_id } })
  } catch (cause) {
    error.value = cause.response?.data?.detail || cause.message || '创建会话失败。'
  }
}

async function sendMessage() {
  const content = draftMessage.value.trim()
  if (!content || !activeConversation.value) return
  const conversationId = activeConversation.value.conversation_id
  error.value = ''
  try {
    pendingClientMessageId.value ??= crypto.randomUUID()
    const result = await conversations.send(content, pendingClientMessageId.value)
    draftMessage.value = ''
    pendingClientMessageId.value = null
    startRunTracking(result.run_id, conversationId)
  } catch (cause) {
    error.value = cause.response?.data?.detail || cause.message || '发送消息失败。'
  }
}

function stopRunPolling(runId = pollingRunId) {
  if (runId !== pollingRunId) return
  if (pollTimer !== null) window.clearInterval(pollTimer)
  pollTimer = null
  pollingRunId = null
  pollingInFlight = false
  isPolling.value = false
}

async function pollRun(runId, conversationId) {
  if (pollingRunId !== runId || pollingInFlight) return
  pollingInFlight = true
  try {
    const run = await conversations.refreshRun(runId)
    error.value = ''
    if (run.status === 'completed' || run.status === 'failed') {
      await finishRunTracking(runId, conversationId)
    }
  } catch (cause) {
    error.value = cause.response?.data?.detail || cause.message || '查询任务状态失败，将自动重试。'
  } finally {
    pollingInFlight = false
  }
}

function startRunPolling(runId, conversationId) {
  if (pollingRunId === runId) return
  stopRunPolling()
  pollingRunId = runId
  isPolling.value = true
  void pollRun(runId, conversationId)
  pollTimer = window.setInterval(() => {
    void pollRun(runId, conversationId)
  }, 1500)
}

function stopRunStream(runId = streamingRunId) {
  if (runId !== streamingRunId) return
  if (streamRetryTimer !== null) window.clearTimeout(streamRetryTimer)
  streamRetryTimer = null
  streamAbortController?.abort()
  streamAbortController = null
  streamingRunId = null
}

function stopRunTracking() {
  stopRunStream()
  stopRunPolling()
  isRunActive.value = false
}

async function finishRunTracking(runId, conversationId) {
  if (runId !== streamingRunId && runId !== pollingRunId) return
  stopRunStream(runId)
  stopRunPolling(runId)
  isRunActive.value = false
  if (activeConversation.value?.conversation_id === conversationId) {
    await conversations.loadConversation(conversationId)
  }
  await conversations.loadHistory()
}

function scheduleRunStreamReconnect(runId, conversationId) {
  if (runId !== streamingRunId || streamRetryTimer !== null) return
  startRunPolling(runId, conversationId)
  streamRetryTimer = window.setTimeout(() => {
    streamRetryTimer = null
    void startRunStream(runId, conversationId)
  }, 2000)
}

async function startRunStream(runId, conversationId) {
  if (streamingRunId !== runId) return
  const controller = new AbortController()
  streamAbortController = controller
  try {
    await subscribeRunEvents(runId, {
      lastEventId: conversations.runEventNos[runId] ?? 0,
      signal: controller.signal,
      onOpen: () => {
        if (streamingRunId === runId) stopRunPolling(runId)
      },
      onEvent: async (sseEvent) => {
        if (streamingRunId !== runId) return
        const handled = conversations.applyRunEvent({
          ...sseEvent.data,
          event_no: sseEvent.data.event_no ?? sseEvent.id,
          event_type: sseEvent.event,
        })
        if (!handled) return
        error.value = ''
        if (['run_completed', 'run_failed', 'approval_required'].includes(sseEvent.event)) {
          await finishRunTracking(runId, conversationId)
        }
      },
    })
    if (streamingRunId === runId && !controller.signal.aborted) {
      scheduleRunStreamReconnect(runId, conversationId)
    }
  } catch (cause) {
    if (streamingRunId !== runId || controller.signal.aborted) return
    error.value = cause.message || '实时任务连接中断，已切换为轮询查询。'
    scheduleRunStreamReconnect(runId, conversationId)
  } finally {
    if (streamAbortController === controller) streamAbortController = null
  }
}

function startRunTracking(runId, conversationId) {
  stopRunTracking()
  isRunActive.value = true
  streamingRunId = runId
  void startRunStream(runId, conversationId)
}

async function signOut() {
  await auth.signOut()
  await router.replace({ name: 'login' })
}

onMounted(async () => {
  await loadHistory()
  const conversationId = route.query.conversation_id
  if (typeof conversationId === 'string' && conversationId) {
    await restoreConversation(conversationId, false)
  }
})

onBeforeUnmount(() => stopRunTracking())
</script>

<template>
  <main class="workspace">
    <aside class="sidebar">
      <div class="brand"><span class="mark">⌾</span>Aegis PA</div>
      <p class="nav-label app-nav-label">工作空间</p>
      <a class="nav active" href="#task-console">◌ 任务对话</a>
      <router-link class="nav" :to="{ name: 'confirmations' }">✓ 操作确认</router-link>
      <span class="nav disabled">◇ 长期记忆（待接入）</span>
      <span class="nav disabled">◫ 连接与审计（待接入）</span>

      <div class="identity">
        <b>{{ auth.profile?.name }}</b>
        <span>{{ auth.profile?.email || '已通过 Keycloak 登录' }}</span>
        <el-button text type="primary" @click="signOut">退出登录</el-button>
      </div>
    </aside>

    <section id="task-console" class="content">
      <header class="page-header">
        <div>
          <h1>{{ activeConversation?.title || '任务对话' }}</h1>
          <p class="muted">{{ activeConversation ? `会话 ID：${activeConversation.conversation_id}` : '从左侧选择一条历史对话，或创建新的任务。' }}</p>
        </div>
        <span class="badge success">● 身份已验证</span>
      </header>

      <div class="task-workspace">
        <aside class="conversation-history" aria-label="历史对话">
          <div class="new-title-editor">
            <label for="conversation-title">对话标题</label>
            <el-input id="conversation-title" v-model="conversationTitle" maxlength="200" placeholder="例如：明日项目同步" />
            <el-button class="new-chat-button" type="primary" :loading="conversations.creating" @click="startNewConversation">
              <span>＋</span> 新建对话
            </el-button>
            <p>可选，不填写时使用“新对话”。</p>
          </div>
          <p class="conversation-history-label">历史对话</p>
          <div v-loading="conversations.loadingHistory" class="history-scroll">
            <button
              v-for="conversation in conversations.history"
              :key="conversation.conversation_id"
              class="history-item"
              :class="{ active: activeConversation?.conversation_id === conversation.conversation_id }"
              type="button"
              @click="restoreConversation(conversation.conversation_id)"
            >
              <span class="history-title">{{ conversation.title }}</span>
              <span class="history-time">{{ formatTime(conversation.last_message_at || conversation.created_at) }}</span>
            </button>
            <p v-if="!conversations.loadingHistory && conversations.history.length === 0" class="history-empty">还没有历史对话。</p>
          </div>
        </aside>

        <div class="console-grid">
          <article v-loading="conversations.loadingDetail" class="card conversation-card">
            <div class="card-head">
              <div>
                <h2>{{ activeConversation ? '已恢复的历史对话' : '开始新的任务' }}</h2>
                <p class="muted">{{ activeConversation ? '已恢复该会话的完整可见消息。' : '选择一条历史会话，或在左侧创建新会话。' }}</p>
              </div>
            </div>

            <el-alert v-if="error" class="conversation-error" :title="error" type="error" :closable="false" show-icon />
            <div v-if="activeConversation" ref="messagesElement" class="messages">
              <template v-for="item in conversations.messages" :key="item.message_id">
                <div class="message" :class="item.role">
                  <div class="avatar">{{ item.role === 'user' ? '你' : 'A' }}</div>
                  <div>
                    <div class="message-bubble">{{ item.content }}</div>
                    <div class="message-meta">{{ item.role === 'user' ? '你' : 'Aegis' }} · {{ formatTime(item.created_at) }}</div>
                  </div>
                </div>
                <template v-if="item.role === 'user' && meetingRunForMessage(item) && meetingPreview(meetingRunForMessage(item))">
                  <div v-for="run in [meetingRunForMessage(item)]" :key="'meeting-plan-' + run.run_id" class="message meeting-plan-message">
                    <div class="avatar">A</div>
                    <div class="meeting-plan-card">
                      <b>{{ meetingPlanHeading(run) }}</b>
                      <p>{{ meetingPreview(run).title || '创建日历事件' }}</p>
                      <dl>
                        <div><dt>开始</dt><dd>{{ formatDateTime(meetingPreview(run).start_at) }}</dd></div>
                        <div><dt>结束</dt><dd>{{ formatDateTime(meetingPreview(run).end_at) }}</dd></div>
                        <div v-if="meetingPreview(run).attendees?.length"><dt>参与者</dt><dd>{{ formatCalendarData(meetingPreview(run).attendees) }}</dd></div>
                        <div v-if="meetingPreview(run).calendar_name"><dt>日历</dt><dd>{{ meetingPreview(run).calendar_name }}</dd></div>
                      </dl>
                      <router-link v-if="canConfirmMeeting(run)" class="run-approval-link" :to="{ name: 'confirmations', query: { run_id: run.run_id } }">核对并逐项确认</router-link>
                      <span v-else-if="meetingPlanOutcome(run)" class="meeting-plan-outcome">{{ meetingPlanOutcome(run) }}</span>
                    </div>
                  </div>
                </template>
              </template>
              <div v-if="conversations.messages.length === 0 && !conversations.runs.some(meetingPreview)" class="empty-messages">此会话暂无可见消息。</div>
              <div v-if="conversations.latestSubmission" class="queued-notice">
                <b>{{ isRunActive ? '任务已提交，正在处理' : '任务状态：' + conversations.latestSubmission.status }}</b>
                <span>任务 ID：{{ conversations.latestSubmission.run_id }}</span>
              </div>
            </div>
            <div v-else class="empty-state">
              <div class="empty-mark">⌁</div>
              <h3>选择历史对话或开始新任务</h3>
              <p>进入页面后已加载当前用户的历史会话；在左侧填写标题并点击“新建对话”即可创建新会话。</p>
            </div>

            <form class="new-conversation" @submit.prevent="sendMessage">
              <el-input v-model="draftMessage" maxlength="8000" placeholder="输入任务，例如：帮我整理今天的邮件" aria-label="输入任务" :disabled="!activeConversation || isRunActive" />
              <el-button native-type="submit" type="primary" :loading="conversations.sending" :disabled="!activeConversation || isRunActive">发送</el-button>
            </form>
          </article>

          <aside>
            <article class="card side-card execution-card">
              <div class="card-head"><h2>执行进度</h2><span class="badge read">{{ conversations.runs.length }} 条运行</span></div>
              <div v-if="conversations.runs.length" class="card-body run-list">
                <section v-for="run in conversations.runs" :key="run.run_id" class="run-item">
                  <div class="run-heading">
                    <b>{{ run.current_stage || '等待处理' }}</b>
                    <span class="badge neutral">{{ run.status }}</span>
                  </div>
                  <p class="run-id">任务 ID：{{ run.run_id }}</p>
                  <p v-if="run.model_provider || run.model_name" class="run-id">模型：{{ [run.model_provider, run.model_name].filter(Boolean).join(' / ') }}</p>
                  <p v-if="run.result_summary" class="run-detail">{{ run.result_summary }}</p>
                  <p v-if="run.error_message" class="run-error">{{ run.error_message }}</p>
                  <ol v-if="run.steps?.length" class="run-steps">
                    <li v-for="step in run.steps" :key="step.step_id">
                      <b>{{ step.label }}</b>
                      <span>{{ step.status }}{{ step.detail ? ' · ' + step.detail : '' }}</span>
                    </li>
                  </ol>
                  <div v-if="run.tool_previews?.length" class="run-section">
                    <b>日历查询结果</b>
                    <div v-for="tool in run.tool_previews" :key="tool.tool_call_id" class="tool-preview">
                      <p><b>{{ tool.tool_name }}</b> · {{ tool.status }} · {{ tool.risk_level }}</p>
                      <p v-if="tool.input_summary">查询条件：{{ formatCalendarData(tool.input_summary) }}</p>
                      <p v-if="tool.output_summary">查询结果：{{ formatCalendarData(tool.output_summary) }}</p>
                      <p v-if="tool.error_message" class="run-error">{{ tool.error_message }}</p>
                    </div>
                  </div>
                  <el-alert
                    v-if="run.connection_required"
                    :title="run.connection_required.message || '需要连接日历账户后才能查询。'"
                    type="warning"
                    :closable="false"
                    show-icon
                  />
                  <div v-if="run.plan_previews?.length" class="run-section calendar-plans">
                    <b>待执行会议计划</b>
                    <div v-for="plan in run.plan_previews" :key="plan.approval_item_id" class="tool-preview">
                      <p><b>{{ plan.preview?.title || '创建日历事件' }}</b> · {{ plan.risk_level || 'write' }}</p>
                      <p>开始：{{ formatDateTime(plan.preview?.start_at) }}</p>
                      <p>结束：{{ formatDateTime(plan.preview?.end_at) }}</p>
                      <p v-if="plan.preview?.attendees?.length">参与者：{{ formatCalendarData(plan.preview.attendees) }}</p>
                      <p v-if="plan.preview?.calendar_name">日历：{{ plan.preview.calendar_name }}</p>
                    </div>
                  </div>
                  <div v-if="run.approval_items?.length" class="run-section">
                    <b>待确认项目</b>
                    <p v-for="approval in run.approval_items" :key="approval.approval_item_id">{{ approval.title }} · {{ approval.status }}</p>
                    <router-link v-if="run.approval_items.some((approval) => approval.status === 'pending')" class="run-approval-link" :to="{ name: 'confirmations', query: { run_id: run.run_id } }">进入逐项确认</router-link>
                  </div>
                  <div v-else-if="run.approval_required" class="run-section">
                    <b>待确认项目已生成</b>
                    <p>{{ run.approval_required.summary || '请核对会议计划后逐项确认。' }}</p>
                    <router-link class="run-approval-link" :to="{ name: 'confirmations', query: { run_id: run.run_id } }">进入逐项确认</router-link>
                  </div>
                </section>
              </div>
              <div v-else class="card-body run-empty">选择历史会话后，将在此恢复关联运行与进度。</div>
            </article>

            <article class="card side-card">
              <div class="card-head"><h2>安全状态</h2><span class="badge read">OIDC + PKCE</span></div>
              <div class="card-body status-list">
                <p><b>访问令牌</b><span>请求前自动刷新并以 Bearer 形式发送。</span></p>
                <p><b>租户身份</b><span>由后端从已验证 token 映射，前端不提交。</span></p>
                <p><b>运行记录</b><span>{{ activeConversation ? `已恢复 ${conversations.runs.length} 条关联运行记录。` : '选择会话后显示关联运行状态。' }}</span></p>
              </div>
            </article>
          </aside>
        </div>
      </div>
    </section>
  </main>
</template>
