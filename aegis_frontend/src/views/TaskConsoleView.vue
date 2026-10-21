<script setup>
import AppNavigation from '../components/AppNavigation.vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { subscribeRunEvents } from '../api/run'
import MailCandidateCard from '../components/MailCandidateCard.vue'
import KnowledgeSourceCard from '../components/KnowledgeSourceCard.vue'
import KnowledgeSourceDialog from '../components/KnowledgeSourceDialog.vue'
import { citationParts, knowledgeRetrievalSummary, knowledgeSearchError } from '../utils/knowledge'
import { cooperationActorLabel, cooperationResultLabel, cooperationStageLabel, groupToolPreviews } from '../utils/cooperation'
import { pendingInputRunsAfterMessage, unanchoredPendingInputRuns } from '../utils/inputRequest'
import { newestRunsFirst } from '../utils/runOrder'
import { runProgressText } from '../utils/runProgress'
import RunInputPanel from '../components/RunInputPanel.vue'
import RunMemoryContextDialog from '../components/RunMemoryContextDialog.vue'
import { useAuthStore } from '../store/auth'
import { useConversationStore } from '../store/conversation'

const auth = useAuthStore()
const conversations = useConversationStore()
const router = useRouter()
const route = useRoute()
const draftMessage = ref('')
const isTemporaryConversation = ref(false)
const pendingClientMessageId = ref(null)
const error = ref('')
const isPolling = ref(false)
const isRunActive = ref(false)
const messagesElement = ref(null)
const inputPanelKey = ref(0)
const selectedSource = ref(null)
const sourceRunId = ref(null)
const memoryRunId = ref(null)
let pollTimer = null
let pollingRunId = null
let pollingInFlight = false
let streamAbortController = null
let streamingRunId = null
let streamRetryTimer = null
let selectionVersion = 0
const replayControllers = new Set()

const activeConversation = computed(() => conversations.current)
watch(() => activeConversation.value?.conversation_id, () => { memoryRunId.value = null })
const isSubmitting = computed(() => conversations.creating || conversations.sending)
const conversationRunIds = computed(() => conversations.runs.map((run) => run.run_id))
const pendingInputRunCount = computed(() => conversations.runs.filter((run) => run.status === 'waiting_input').length)
const recentRuns = computed(() => newestRunsFirst(conversations.runs))
const currentProgressRun = computed(() => {
  const newest = recentRuns.value[0]
  return newest && !['completed', 'failed', 'cancelled'].includes(newest.status) ? newest : null
})
const currentProgressText = computed(() => runProgressText(
  currentProgressRun.value,
  currentProgressRun.value ? cooperationTimeline(currentProgressRun.value.run_id) : [],
))
const progressAnimating = computed(() => ['queued', 'running'].includes(currentProgressRun.value?.status))

watch(() => [conversations.messages.length, currentProgressText.value], async () => {
  const element = messagesElement.value
  if (!element || element.scrollHeight - element.scrollTop - element.clientHeight > 80) return
  await nextTick()
  if (messagesElement.value === element) element.scrollTop = element.scrollHeight
})

function scrollToPendingInput() {
  const run = conversations.runs.find((item) => item.status === 'waiting_input')
  const card = run && messagesElement.value?.querySelector(`#run-input-${run.run_id}`)
  card?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function cooperationTimeline(runId) {
  return conversations.cooperationByRun[runId] ?? []
}

function currentCooperation(runId) {
  return cooperationTimeline(runId).at(-1)
}

function cooperationHandler(run) {
  if (['completed', 'failed', 'cancelled'].includes(run.status)) {
    return cooperationActorLabel(currentCooperation(run.run_id)?.cooperation?.actor)
  }
  if (run.status === 'waiting_input') return '等待用户补充'
  if (run.status === 'waiting_confirmation') return '等待用户确认'
  if (currentCooperation(run.run_id)?.phase === 'finished') return '等待后续确认'
  return cooperationActorLabel(currentCooperation(run.run_id)?.cooperation?.actor)
}

function resumeInputRun(runId) {
  if (activeConversation.value && conversationRunIds.value.includes(runId)) {
    startRunTracking(runId, activeConversation.value.conversation_id)
    void conversations.refreshRun(runId).catch(() => {
      // SSE 或后续轮询仍会同步权威状态。
    })
  }
}

function cancelInputRun(runId) {
  if (activeConversation.value && conversationRunIds.value.includes(runId)) {
    void conversations.refreshRun(runId).catch(() => {
      // SSE 或下次进入会话时会恢复权威状态。
    })
  }
}

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

function meetingRecommendation(preview) {
  return preview?.recommendation ?? null
}

function formatRecommendationSlot(slot) {
  if (!slot?.start_at && !slot?.end_at) return slot?.slot_id ?? '—'
  return `${formatDateTime(slot.start_at)} ～ ${formatDateTime(slot.end_at)}`
}

function meetingRunForMessage(message) {
  if (!message.run_id) return null
  return conversations.runs.find((run) => run.run_id === message.run_id) ?? null
}

function artifactsForMessage(message) {
  if (!message.run_id) return []
  return (conversations.artifactsByRun[message.run_id] ?? [])
    .filter((artifact) => {
      if (!['calendar_events', 'calendar_free_time', 'mail_candidates', 'knowledge_sources'].includes(artifact.artifact_type)) return false
      if (artifact.assistant_message_id) return artifact.assistant_message_id === message.message_id
      return message.role === 'user'
    })
}

function artifactTitle(artifact) {
  return artifact.artifact_type === 'calendar_free_time' ? '可用时间' : '日程结果'
}

function messageParts(message) {
  const sources = message.role === 'assistant' ? artifactsForMessage(message)
    .filter((artifact) => artifact.artifact_type === 'knowledge_sources')
    .flatMap((artifact) => artifact.payload?.sources ?? []) : []
  return citationParts(message.content, sources)
}

function openSource(source, runId) {
  sourceRunId.value = runId
  selectedSource.value = source
}

async function recheckSource() {
  if (sourceRunId.value) await refreshArtifactsForRun(sourceRunId.value, activeConversation.value?.conversation_id)
}

function refreshVisibleKnowledge() {
  selectedSource.value = null
  const id = activeConversation.value?.conversation_id
  if (!id) return
  for (const [runId, artifacts] of Object.entries(conversations.artifactsByRun)) {
    if (artifacts.some((artifact) => artifact.artifact_type === 'knowledge_sources')) void refreshArtifactsForRun(runId, id)
  }
}

watch(() => activeConversation.value?.conversation_id, () => { selectedSource.value = null })
onMounted(() => window.addEventListener('focus', refreshVisibleKnowledge))
onBeforeUnmount(() => window.removeEventListener('focus', refreshVisibleKnowledge))

function artifactItems(artifact) {
  return artifact.payload?.items ?? []
}

async function loadArtifactsForConversation(detail) {
  const conversationId = detail?.conversation_id
  if (!conversationId) return
  await Promise.allSettled((detail.runs ?? []).map(
    (run) => conversations.loadArtifacts(conversationId, run.run_id),
  ))
}

async function refreshArtifactsForRun(runId, conversationId) {
  if (!conversationId || !runId) return
  try {
    await conversations.loadArtifacts(conversationId, runId)
  } catch {
    // Artifact 是可选展示结果；失败不覆盖任务本身的状态或错误信息。
  }
}

function isTrackableRun(run) {
  return !['completed', 'failed', 'cancelled', 'waiting_confirmation', 'waiting_approval']
    .includes(run.status)
}

async function scrollMessagesToBottom() {
  await nextTick()
  const element = messagesElement.value
  if (!element) return
  element.scrollTop = element.scrollHeight
  // 等待浏览器完成布局，避免首次进入页面时卡片撑高消息区后又停在顶部。
  window.requestAnimationFrame(() => {
    if (messagesElement.value === element) element.scrollTop = element.scrollHeight
  })
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
  const selection = ++selectionVersion
  stopRunTracking()
  isTemporaryConversation.value = false
  draftMessage.value = ''
  pendingClientMessageId.value = null
  error.value = ''
  try {
    const detail = await conversations.loadConversation(conversationId)
    if (!detail || selection !== selectionVersion) return
    await loadArtifactsForConversation(detail)
    if (selection !== selectionVersion) return
    draftMessage.value = ''
    pendingClientMessageId.value = null
    const activeRun = detail.runs?.find(isTrackableRun)
      ?? detail.runs?.find((run) => run.status === 'waiting_confirmation')
    if (activeRun) startRunTracking(activeRun.run_id, conversationId, {
      locksComposer: activeRun.status !== 'waiting_confirmation',
    })
    void replayOtherRuns((detail.runs ?? []).filter((run) =>
      run.run_id !== activeRun?.run_id), selection)
    if (syncUrl) {
      await router.replace({ query: { conversation_id: conversationId } })
    }
    await scrollMessagesToBottom()
  } catch (cause) {
    if (selection === selectionVersion) {
      error.value = cause.response?.data?.detail || cause.message || '恢复会话失败。'
    }
  }
}

async function startNewConversation() {
  if (isSubmitting.value) return
  ++selectionVersion
  stopRunTracking()
  error.value = ''
  conversations.clearCurrent()
  isTemporaryConversation.value = true
  draftMessage.value = ''
  pendingClientMessageId.value = null
  await router.replace({ name: 'tasks' })
}

async function sendMessage() {
  const content = draftMessage.value.trim()
  if (!content || (!activeConversation.value && !isTemporaryConversation.value) || isSubmitting.value) return
  error.value = ''
  try {
    if (isTemporaryConversation.value) {
      const title = content.replace(/\s+/g, ' ').slice(0, 200)
      await conversations.start(title)
      isTemporaryConversation.value = false
      await router.replace({ name: 'tasks', query: { conversation_id: conversations.current.conversation_id } })
    }
    const conversationId = conversations.current.conversation_id
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
    if (['completed', 'failed', 'cancelled'].includes(run.status)) {
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

function stopRunTracking({ stopReplays = true } = {}) {
  stopRunStream()
  stopRunPolling()
  if (stopReplays) {
    for (const controller of replayControllers) controller.abort()
    replayControllers.clear()
  }
  isRunActive.value = false
}

async function replayOtherRuns(runs, selection) {
  let next = 0
  async function worker() {
    while (next < runs.length && selection === selectionVersion) {
      const run = runs[next++]
      const controller = new AbortController()
      replayControllers.add(controller)
      // 终态任务由服务端结束流；缺少终态事件或仍在等待的任务在回放空闲后释放连接。
      let idleTimer = window.setTimeout(() => controller.abort(), 10000)
      const maxTimer = ['completed', 'failed', 'cancelled'].includes(run.status)
        ? null : window.setTimeout(() => controller.abort(), 30000)
      try {
        await subscribeRunEvents(run.run_id, {
          lastEventId: 0,
          signal: controller.signal,
          onEvent: (frame) => {
            if (selection !== selectionVersion || frame.data?.run_id !== run.run_id) return
            conversations.applyRunEvent({
              ...frame.data,
              event_no: frame.data.event_no ?? frame.id,
              event_type: frame.event,
            }, { historyOnly: true })
            window.clearTimeout(idleTimer)
            idleTimer = window.setTimeout(() => controller.abort(), 3000)
          },
        })
      } catch {
        // 历史时间线为可选展示；任务状态仍以会话详情为准。
      } finally {
        window.clearTimeout(idleTimer)
        if (maxTimer !== null) window.clearTimeout(maxTimer)
        replayControllers.delete(controller)
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(3, runs.length) }, worker))
}

async function finishRunTracking(runId, conversationId) {
  if (runId !== streamingRunId && runId !== pollingRunId) return
  stopRunStream(runId)
  stopRunPolling(runId)
  isRunActive.value = false
  if (activeConversation.value?.conversation_id === conversationId) {
    const detail = await conversations.loadConversation(conversationId, { preserveCooperation: true })
    await loadArtifactsForConversation(detail)
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
        if (streamingRunId !== runId || sseEvent.data?.run_id !== runId) return
        const handled = conversations.applyRunEvent({
          ...sseEvent.data,
          event_no: sseEvent.data.event_no ?? sseEvent.id,
          event_type: sseEvent.event,
        })
        if (!handled) return
        error.value = ''
        if (sseEvent.event === 'input_required') {
          inputPanelKey.value += 1
        }
        if (['artifacts_ready', 'assistant_message_completed'].includes(sseEvent.event)) {
          await refreshArtifactsForRun(runId, sseEvent.data?.payload?.conversation_id ?? conversationId)
        }
        if (sseEvent.event === 'approval_required') isRunActive.value = false
        if (['run_completed', 'run_failed', 'run_cancelled'].includes(sseEvent.event)) {
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

function startRunTracking(runId, conversationId, { locksComposer = true } = {}) {
  stopRunTracking({ stopReplays: false })
  isRunActive.value = locksComposer
  streamingRunId = runId
  void startRunStream(runId, conversationId)
}

async function signOut() {
  await auth.signOut()
  await router.replace({ name: 'login' })
}

onMounted(async () => {
  conversations.clearCurrent()
  await loadHistory()
  const conversationId = route.query.conversation_id
  if (typeof conversationId === 'string' && conversationId) {
    await restoreConversation(conversationId, false)
  }
})

watch(() => route.query.conversation_id, (conversationId) => {
  if (typeof conversationId === 'string' && conversationId
    && conversationId !== activeConversation.value?.conversation_id) {
    void restoreConversation(conversationId, false)
  } else if (!conversationId && !isTemporaryConversation.value) {
    ++selectionVersion
    stopRunTracking()
    conversations.clearCurrent()
  }
})

onBeforeUnmount(() => { stopRunTracking() })
</script>

<template>
  <main class="workspace">
    <KnowledgeSourceDialog :source="selectedSource" @close="selectedSource = null" @unavailable="recheckSource" />
    <aside class="sidebar">
      <div class="brand">Aegis PA</div>
      <AppNavigation active="tasks" />

      <div class="identity">
        <b>{{ auth.profile?.name }}</b>
        <span>{{ auth.profile?.email || '已通过 Keycloak 登录' }}</span>
        <el-button text type="primary" @click="signOut">退出登录</el-button>
      </div>
    </aside>

    <section id="task-console" class="content">
      <header class="page-header">
        <div>
          <h1>{{ isTemporaryConversation ? '新对话' : activeConversation?.title || '任务对话' }}</h1>
          <p class="muted">{{ isTemporaryConversation ? '发送第一条消息后才会创建会话，并以这条消息作为标题。' : activeConversation ? `会话 ID：${activeConversation.conversation_id}` : '从左侧选择一条历史对话，或开始新对话。' }}</p>
        </div>
        <span class="badge success">● 身份已验证</span>
      </header>

      <div class="task-workspace">
        <aside class="conversation-history" aria-label="历史对话">
          <button class="new-chat-entry" :class="{ active: isTemporaryConversation }" type="button" :disabled="isSubmitting" @click="startNewConversation">
            <span class="new-chat-icon" aria-hidden="true">＋</span>
            <span>新建对话</span>
          </button>
          <p class="conversation-history-label">历史对话</p>
          <div v-loading="conversations.loadingHistory" class="history-scroll">
            <button
              v-for="conversation in conversations.history"
              :key="conversation.conversation_id"
              class="history-item"
              :class="{ active: activeConversation?.conversation_id === conversation.conversation_id }"
              type="button"
              :disabled="isSubmitting"
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
                <h2>{{ isTemporaryConversation ? '新对话' : activeConversation ? '已恢复的历史对话' : '开始新的任务' }}</h2>
                <p class="muted">{{ isTemporaryConversation ? '输入并发送第一条消息后，这条对话才会保存到历史记录。' : activeConversation ? '已恢复该会话的完整可见消息。' : '选择一条历史会话，或点击左侧加号开始新对话。' }}</p>
              </div>
              <el-button v-if="pendingInputRunCount" size="small" @click="scrollToPendingInput">待回答问题 {{ pendingInputRunCount }}</el-button>
            </div>

            <el-alert v-if="error" class="conversation-error" :title="error" type="error" :closable="false" show-icon />
            <div v-if="activeConversation || isTemporaryConversation" ref="messagesElement" class="messages">
              <template v-for="(item, messageIndex) in conversations.messages" :key="item.message_id">
                <div class="message" :class="item.role">
                  <div class="avatar">{{ item.role === 'user' ? '你' : 'A' }}</div>
                  <div>
                    <div class="message-bubble"><template v-for="(part, partIndex) in messageParts(item)" :key="partIndex"><button v-if="part.source" type="button" class="knowledge-citation" @click="openSource(part.source, item.run_id)">{{ part.text }}</button><template v-else>{{ part.text }}</template></template></div>
                    <div class="message-meta">{{ item.role === 'user' ? '你' : 'Aegis' }} · {{ formatTime(item.created_at) }}</div>
                  </div>
                </div>
                <div
                  v-for="artifact in artifactsForMessage(item)"
                  :key="'artifact-' + artifact.artifact_id"
                  class="message artifact-message"
                >
                  <div class="avatar">A</div>
                  <MailCandidateCard v-if="artifact.artifact_type === 'mail_candidates'" :artifact="artifact" @input-required="inputPanelKey += 1" />
                  <KnowledgeSourceCard v-else-if="artifact.artifact_type === 'knowledge_sources'" :artifact="artifact" :error="conversations.artifactErrors[item.run_id] || ''" @open-source="openSource($event, item.run_id)" @refresh="refreshArtifactsForRun(item.run_id, activeConversation?.conversation_id)" />
                  <div v-else class="artifact-card">
                    <div class="artifact-heading">
                      <b>{{ artifactTitle(artifact) }}</b>
                      <span v-if="artifact.payload?.display_timezone">{{ artifact.payload.display_timezone }}</span>
                    </div>
                    <p v-if="artifact.artifact_type === 'calendar_free_time' && artifact.payload?.duration_minutes">
                      建议会议时长：{{ artifact.payload.duration_minutes }} 分钟
                    </p>
                    <div v-for="(entry, index) in artifactItems(artifact)" :key="index" class="artifact-item">
                      <b>{{ entry.title || '可用时段' }}</b>
                      <span>{{ entry.start_at_local || entry.start_at || '—' }} ～ {{ entry.end_at_local || entry.end_at || '—' }}</span>
                    </div>
                    <p v-if="!artifactItems(artifact).length" class="muted">暂无可展示的脱敏结果。</p>
                  </div>
                </div>
                <p v-if="item.role === 'assistant' && conversations.artifactErrors[item.run_id] && !artifactsForMessage(item).some((artifact) => artifact.artifact_type === 'knowledge_sources')" role="alert" class="muted">展示结果暂不可用，未使用旧缓存。<button type="button" class="knowledge-citation" @click="refreshArtifactsForRun(item.run_id, activeConversation?.conversation_id)">重新读取卡片</button></p>
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
                      <div v-if="meetingRecommendation(meetingPreview(run))" class="meeting-recommendation">
                        <b>推荐原因</b>
                        <span v-if="meetingRecommendation(meetingPreview(run)).reason">{{ meetingRecommendation(meetingPreview(run)).reason }}</span>
                        <template v-if="meetingRecommendation(meetingPreview(run)).alternative_slots?.length">
                          <b>备选时间</b>
                          <span v-for="slot in meetingRecommendation(meetingPreview(run)).alternative_slots" :key="slot.slot_id || `${slot.start_at}-${slot.end_at}`">
                            {{ formatRecommendationSlot(slot) }}
                          </span>
                        </template>
                      </div>
                      <router-link v-if="canConfirmMeeting(run)" class="run-approval-link" :to="{ name: 'confirmations', query: { run_id: run.run_id } }">核对并逐项确认</router-link>
                      <span v-else-if="meetingPlanOutcome(run)" class="meeting-plan-outcome">{{ meetingPlanOutcome(run) }}</span>
                    </div>
                  </div>
                </template>
                <RunInputPanel
                  v-for="run in pendingInputRunsAfterMessage(conversations.messages, conversations.runs, messageIndex)"
                  :key="`input-${run.run_id}-${inputPanelKey}`"
                  :run-id="run.run_id"
                  scope="conversation"
                  :conversation-id="activeConversation?.conversation_id"
                  inline
                  @answered="resumeInputRun"
                  @cancelled="cancelInputRun"
                />
              </template>
              <RunInputPanel
                v-for="run in unanchoredPendingInputRuns(conversations.messages, conversations.runs)"
                :key="`unanchored-input-${run.run_id}-${inputPanelKey}`"
                :run-id="run.run_id"
                scope="conversation"
                :conversation-id="activeConversation?.conversation_id"
                inline
                @answered="resumeInputRun"
                @cancelled="cancelInputRun"
              />
              <div v-if="isTemporaryConversation" class="empty-messages">新对话尚未创建。发送第一条消息后才会保存。</div>
              <div v-else-if="conversations.messages.length === 0 && !conversations.runs.some(meetingPreview) && !pendingInputRunCount" class="empty-messages">此会话暂无可见消息。</div>
              <div v-if="currentProgressText" class="conversation-progress" :class="{ active: progressAnimating }" role="status" aria-live="polite">
                <span class="progress-indicator" aria-hidden="true"></span>
                <span>{{ currentProgressText }}</span>
                <span v-if="progressAnimating" class="progress-activity" aria-hidden="true"><i></i><i></i><i></i></span>
              </div>
            </div>
            <div v-else class="empty-state">
              <div class="empty-mark">⌁</div>
              <h3>选择历史对话或开始新任务</h3>
              <p>进入页面后已加载当前用户的历史会话；点击左侧加号开始新对话，发送第一条消息后才会保存。</p>
            </div>

            <form class="new-conversation" @submit.prevent="sendMessage">
              <el-input v-model="draftMessage" maxlength="8000" placeholder="输入任务，例如：帮我整理今天的邮件" aria-label="输入任务" :disabled="(!activeConversation && !isTemporaryConversation) || isRunActive || isSubmitting" />
              <el-button native-type="submit" type="primary" :loading="isSubmitting" :disabled="(!activeConversation && !isTemporaryConversation) || isRunActive || isSubmitting || !draftMessage.trim()">发送</el-button>
            </form>
          </article>

          <aside>
            <article class="card side-card execution-card">
              <div class="card-head"><h2>执行进度</h2><span class="badge read">{{ conversations.runs.length }} 条运行</span></div>
              <div v-if="conversations.runs.length" class="card-body run-list">
                <section v-for="run in recentRuns" :key="run.run_id" class="run-item">
                  <div class="run-heading">
                    <b>{{ run.current_stage || '等待处理' }}</b>
                    <span class="badge neutral">{{ run.status }}</span>
                  </div>
                  <p class="run-id">任务 ID：{{ run.run_id }}</p>
                  <div v-if="run.created_at || run.started_at || run.finished_at" class="run-times">
                    <span v-if="run.created_at">提交 <time :datetime="run.created_at">{{ formatDateTime(run.created_at) }}</time></span>
                    <span v-if="run.started_at">开始 <time :datetime="run.started_at">{{ formatDateTime(run.started_at) }}</time></span>
                    <span v-if="run.finished_at">结束 <time :datetime="run.finished_at">{{ formatDateTime(run.finished_at) }}</time></span>
                  </div>
                  <el-button size="small" text @click="memoryRunId = run.run_id">查看记忆使用情况</el-button>
                  <p v-if="run.model_provider || run.model_name" class="run-id">模型：{{ [run.model_provider, run.model_name].filter(Boolean).join(' / ') }}</p>
                  <p v-if="run.result_summary" class="run-detail">{{ run.result_summary }}</p>
                  <p v-if="run.error_message" class="run-error">{{ run.error_message }}</p>
                  <div v-if="cooperationTimeline(run.run_id).length" class="run-section cooperation-section">
                    <div class="cooperation-header">
                      <b>协作进度</b>
                      <span v-if="currentCooperation(run.run_id)">
                        {{ ['completed', 'failed', 'cancelled'].includes(run.status) ? '最近处理者' : '当前处理者' }}：{{ cooperationHandler(run) }}
                      </span>
                    </div>
                    <ol class="cooperation-timeline">
                      <li v-for="item in cooperationTimeline(run.run_id)" :key="item.logical_id">
                        <span class="cooperation-dot" aria-hidden="true"></span>
                        <div>
                          <b>{{ cooperationStageLabel(item) }}</b>
                          <small v-if="item.cooperation?.round != null || item.cooperation?.sequence">
                            <template v-if="item.cooperation?.round != null">第 {{ item.cooperation.round }} 轮</template>
                            <template v-if="item.cooperation?.sequence"> · 委派 {{ item.cooperation.sequence }}</template>
                          </small>
                          <small v-if="item.created_at">{{ formatDateTime(item.created_at) }}</small>
                          <p v-if="item.result_status">专家结果：{{ cooperationResultLabel(item.result_status) }}</p>
                          <p v-if="item.error_code" class="run-error">错误码：{{ item.error_code }}</p>
                        </div>
                      </li>
                    </ol>
                    <p v-if="['draft_ready', 'finished'].includes(currentCooperation(run.run_id)?.phase) && !['completed', 'failed', 'cancelled'].includes(run.status)" class="cooperation-caution">仅代表站内草稿或生成阶段完成；会议尚未确认或写入日历。</p>
                  </div>
                  <ol v-if="run.steps?.length" class="run-steps">
                    <li v-for="step in run.steps" :key="step.step_id">
                      <b>{{ step.label }}</b>
                      <span>{{ step.status }}{{ step.detail ? ' · ' + step.detail : '' }}</span>
                    </li>
                  </ol>
                  <div v-if="run.tool_previews?.length" class="run-section">
                    <b>工具查询结果</b>
                    <div v-for="group in groupToolPreviews(run.tool_previews)" :key="group.key" class="tool-group">
                      <p class="tool-group-title">{{ group.label }}</p>
                      <div v-for="tool in group.tools" :key="tool.tool_call_id" class="tool-preview">
                      <p><b>{{ tool.tool_name }}</b> · {{ tool.status }} · {{ tool.risk_level }}</p>
                      <template v-if="tool.tool_name === 'knowledge.search'">
                        <template v-if="tool.output_summary">
                          <p>检索方式：{{ knowledgeRetrievalSummary(tool.output_summary).mode }}</p>
                          <p v-if="knowledgeRetrievalSummary(tool.output_summary).count !== null">已核验证据：{{ knowledgeRetrievalSummary(tool.output_summary).count }} 条</p>
                          <p v-if="knowledgeRetrievalSummary(tool.output_summary).reason" :class="{ 'run-error': knowledgeRetrievalSummary(tool.output_summary).degraded }">{{ knowledgeRetrievalSummary(tool.output_summary).reason }}</p>
                        </template>
                        <p v-if="tool.status === 'failed'" class="run-error">{{ knowledgeSearchError(tool) }}</p>
                      </template>
                      <template v-else>
                        <p v-if="tool.input_summary">查询条件：{{ formatCalendarData(tool.input_summary) }}</p>
                        <p v-if="tool.output_summary">查询结果：{{ formatCalendarData(tool.output_summary) }}</p>
                        <p v-if="tool.error_message" class="run-error">{{ tool.error_message }}</p>
                      </template>
                      </div>
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
                      <p v-if="meetingRecommendation(plan.preview)?.reason">推荐原因：{{ meetingRecommendation(plan.preview).reason }}</p>
                      <template v-if="meetingRecommendation(plan.preview)?.alternative_slots?.length">
                        <p>备选时间：</p>
                        <ul class="run-alternatives">
                          <li v-for="slot in meetingRecommendation(plan.preview).alternative_slots" :key="slot.slot_id || `${slot.start_at}-${slot.end_at}`">
                            {{ formatRecommendationSlot(slot) }}
                          </li>
                        </ul>
                      </template>
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
    <RunMemoryContextDialog :run-id="memoryRunId" @close="memoryRunId = null" />
  </main>
</template>

<style scoped>
.knowledge-citation { color:#1c5bd6;background:transparent;border:0;font:inherit;cursor:pointer;padding:0 2px; }
.conversation-progress { display: flex; align-items: center; gap: 8px; align-self: flex-start; max-width: 100%; margin: 2px 0 0 41px; color: #667085; font-size: 12px; line-height: 1.5; }
.progress-indicator { position: relative; width: 7px; height: 7px; flex: none; border-radius: 50%; background: #5b92d0; box-shadow: 0 0 0 3px #e9f2ff; }
.conversation-progress.active .progress-indicator::after { position: absolute; inset: -4px; border: 1px solid #5b92d0; border-radius: 50%; content: ''; animation: progress-pulse 1.8s ease-out infinite; }
.progress-activity { display: inline-flex; align-items: center; gap: 3px; margin-left: -4px; }
.progress-activity i { width: 3px; height: 3px; border-radius: 50%; background: currentColor; animation: progress-dots 1.2s ease-in-out infinite; }
.progress-activity i:nth-child(2) { animation-delay: .15s; }
.progress-activity i:nth-child(3) { animation-delay: .3s; }
@keyframes progress-pulse { 0% { transform: scale(.55); opacity: .65; } 100% { transform: scale(1.7); opacity: 0; } }
@keyframes progress-dots { 0%, 70%, 100% { transform: translateY(0); opacity: .35; } 35% { transform: translateY(-3px); opacity: 1; } }
@media (prefers-reduced-motion: reduce) {
  .conversation-progress.active .progress-indicator::after, .progress-activity i { animation: none; }
  .progress-activity i { opacity: .65; }
}
.cooperation-section { background: #f5f8fd; }
.cooperation-header { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
.cooperation-header span { color: #52637d; font-size: 11px; }
.cooperation-timeline { list-style: none; margin: 10px 0 0; padding: 0; }
.cooperation-timeline li { display: flex; gap: 8px; padding-bottom: 9px; color: #344054; }
.cooperation-timeline li:last-child { padding-bottom: 0; }
.cooperation-dot { flex: none; width: 8px; height: 8px; margin-top: 5px; border-radius: 50%; background: #4988d8; }
.cooperation-timeline li > div { min-width: 0; }
.cooperation-timeline b { font-size: 12px; }
.cooperation-timeline small { display: block; color: #667085; font-size: 11px; }
.cooperation-timeline p { margin: 2px 0 0; }
.cooperation-caution { color: #805b19 !important; }
.tool-group { margin-top: 8px; padding-left: 8px; border-left: 2px solid #c9d9f0; }
.tool-group-title { margin: 0 !important; color: #52637d !important; font-weight: 700; }
</style>
