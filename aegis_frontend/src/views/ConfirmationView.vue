<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getApprovalItem, listApprovalItems, submitApprovalDecision } from '../api/approval'
import { subscribeRunEvents } from '../api/run'
import { useAuthStore } from '../store/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const items = ref([])
const checkedById = ref({})
const loading = ref(false)
const decidingItemId = ref(null)
const error = ref('')
const executionByApprovalId = ref({})
const runEventNos = ref({})
const streamControllers = new Map()
const reconnectTimers = new Map()

const runId = computed(() => (
  typeof route.query.run_id === 'string' ? route.query.run_id : ''
))
const pendingCount = computed(() => items.value.filter((item) => item.status === 'pending').length)

const labels = {
  title: '会议主题',
  start_at: '开始时间',
  end_at: '结束时间',
  timezone: '时区',
  attendees: '参与者',
  calendar_name: '日历',
  calendar_id: '日历',
  description: '说明',
  to: '收件人',
  cc: '抄送',
  subject: '主题',
  body_preview: '邮件正文预览',
  draft_version: '草稿版本',
}

function formatValue(value) {
  if (Array.isArray(value)) return value.join('、')
  if (value && typeof value === 'object') return JSON.stringify(value)
  return String(value ?? '—')
}

function previewEntries(item) {
  return Object.entries(item.preview_snapshot ?? {}).map(([key, value]) => ({
    key,
    label: labels[key] ?? key,
    value: formatValue(value),
  }))
}

function actionVersion(item) {
  return item.draft_version ? '草稿版本 v' + item.draft_version : item.action
}

function statusText(status) {
  return {
    pending: '待确认',
    approved_executing: '已批准，正在执行',
    rejected: '已拒绝',
    expired: '已过期',
    executed: '已执行',
    failed: '执行失败',
  }[status] ?? status
}

function approvalLabel(item) {
  if (item.action === 'calendar.create_event') return '批准并创建日历事件'
  if (item.action === 'mail.messages.send') return '批准并发送邮件'
  return '批准并执行'
}

function actionNoun(item) {
  if (item?.action === 'mail.messages.send') return '邮件'
  if (item?.action === 'calendar.create_event') return '日历事件'
  return '操作'
}

async function loadItems() {
  loading.value = true
  error.value = ''
  try {
    items.value = await listApprovalItems({
      runId: runId.value || undefined,
      status: 'pending',
    })
  } catch (cause) {
    error.value = cause.response?.data?.detail || cause.message || '加载待确认操作失败。'
  } finally {
    loading.value = false
  }
}

function setExecutionState(approvalItemId, status, message, providerResourceId = null) {
  executionByApprovalId.value = {
    ...executionByApprovalId.value,
    [approvalItemId]: { status, message, providerResourceId },
  }
}

async function refreshApprovalItemsForRun(targetRunId) {
  const matchingItems = items.value.filter((item) => item.run_id === targetRunId)
  await Promise.all(matchingItems.map(async (item) => {
    const refreshed = await getApprovalItem(item.approval_item_id)
    const index = items.value.findIndex(
      (candidate) => candidate.approval_item_id === item.approval_item_id,
    )
    if (index !== -1) items.value.splice(index, 1, refreshed)
  }))
}

function stopRunStream(targetRunId) {
  reconnectTimers.get(targetRunId) && window.clearTimeout(reconnectTimers.get(targetRunId))
  reconnectTimers.delete(targetRunId)
  streamControllers.get(targetRunId)?.abort()
  streamControllers.delete(targetRunId)
}

function stopAllRunStreams() {
  for (const targetRunId of [...streamControllers.keys(), ...reconnectTimers.keys()]) {
    stopRunStream(targetRunId)
  }
}

function scheduleRunStreamReconnect(targetRunId) {
  if (reconnectTimers.has(targetRunId) || streamControllers.has(targetRunId)) return
  reconnectTimers.set(targetRunId, window.setTimeout(() => {
    reconnectTimers.delete(targetRunId)
    void subscribeToRunExecution(targetRunId)
  }, 1500))
}

async function subscribeToRunExecution(targetRunId) {
  if (streamControllers.has(targetRunId)) return
  const controller = new AbortController()
  let terminal = false
  streamControllers.set(targetRunId, controller)
  try {
    await subscribeRunEvents(targetRunId, {
      lastEventId: runEventNos.value[targetRunId] ?? 0,
      signal: controller.signal,
      onEvent: async (sseEvent) => {
        const wireEvent = sseEvent.data ?? {}
        const payload = wireEvent.payload ?? {}
        const eventNo = Number(wireEvent.event_no ?? sseEvent.id)
        if (Number.isSafeInteger(eventNo)) {
          if (eventNo <= (runEventNos.value[targetRunId] ?? 0)) return
          runEventNos.value = { ...runEventNos.value, [targetRunId]: eventNo }
        }

        if (sseEvent.event === 'approval_executed') {
          setExecutionState(
            payload.approval_item_id,
            'executed',
            actionNoun(items.value.find((item) => item.approval_item_id === payload.approval_item_id)) + '已执行。',
            payload.provider_resource_id,
          )
          await refreshApprovalItemsForRun(targetRunId)
        }

        if (sseEvent.event === 'run_failed') {
          terminal = true
          items.value
            .filter((item) => item.run_id === targetRunId)
            .forEach((item) => setExecutionState(
              item.approval_item_id,
              'failed',
              payload.error_message ?? payload.message ?? '操作执行失败。',
            ))
          await refreshApprovalItemsForRun(targetRunId)
        }

        if (sseEvent.event === 'run_completed') {
          terminal = true
          await refreshApprovalItemsForRun(targetRunId)
        }
      },
    })
  } catch (cause) {
    if (!controller.signal.aborted) {
      error.value = cause.message || '执行状态连接中断，正在重连。'
    }
  } finally {
    if (streamControllers.get(targetRunId) === controller) {
      streamControllers.delete(targetRunId)
    }
    if (!terminal && !controller.signal.aborted) scheduleRunStreamReconnect(targetRunId)
  }
}

async function decide(item, decision) {
  if (decision === 'approved' && !checkedById.value[item.approval_item_id]) {
    error.value = '请先勾选已核对该项操作，再批准。'
    return
  }
  decidingItemId.value = item.approval_item_id
  error.value = ''
  try {
    const result = await submitApprovalDecision(item.approval_item_id, decision)
    const index = items.value.findIndex(
      (candidate) => candidate.approval_item_id === item.approval_item_id,
    )
    if (index !== -1) {
      items.value.splice(index, 1, {
        ...items.value[index],
        status: result.approval_status,
      })
    }
    if (result.execution_scheduled) {
      setExecutionState(
        item.approval_item_id,
        'executing',
        '已登记执行，正在由服务端处理。',
      )
    } else {
      setExecutionState(item.approval_item_id, 'rejected', '已拒绝，该操作不会执行。')
    }
    void subscribeToRunExecution(result.run_id)
  } catch (cause) {
    error.value = cause.response?.data?.detail || cause.message || '提交确认决定失败。'
  } finally {
    decidingItemId.value = null
  }
}

async function signOut() {
  await auth.signOut()
  await router.replace({ name: 'login' })
}

watch(runId, () => {
  stopAllRunStreams()
  void loadItems()
}, { immediate: true })
onBeforeUnmount(stopAllRunStreams)
</script>

<template>
  <main class="workspace">
    <aside class="sidebar">
      <div class="brand"><span class="mark">⌾</span>Aegis PA</div>
      <p class="nav-label app-nav-label">工作空间</p>
      <router-link class="nav" :to="{ name: 'tasks' }">◌ 任务对话</router-link>
      <router-link class="nav" :to="{ name: 'mail-management' }">✉ 邮件管理</router-link>
      <router-link class="nav" :to="{ name: 'todo-plans' }">☷ 待办计划</router-link>
      <router-link class="nav active" :to="{ name: 'confirmations' }">✓ 操作确认</router-link>
      <span class="nav disabled">◇ 长期记忆（待接入）</span>
      <span class="nav disabled">◫ 连接与审计（待接入）</span>

      <div class="identity">
        <b>{{ auth.profile?.name }}</b>
        <span>{{ auth.profile?.email || '已通过 Keycloak 登录' }}</span>
        <el-button text type="primary" @click="signOut">退出登录</el-button>
      </div>
    </aside>

    <section class="content confirmation-content">
      <header class="page-header">
        <div>
          <h1>逐项确认待执行操作</h1>
          <p class="muted">{{ runId ? '正在显示本次任务的待确认操作。' : '正在显示你的全部待确认操作。' }}</p>
        </div>
        <span class="badge sensitive">{{ pendingCount }} 项待确认</span>
      </header>

      <div class="approval-notice">
        <b>确认规则：</b>
        每项操作独立确认、独立审计。批准后才会恢复该任务；仅当日历服务返回成功结果时，才表示日历事件已创建。
      </div>

      <article v-loading="loading" class="card approval-card">
        <div class="card-head">
          <div>
            <h2>待执行任务列表</h2>
            <p class="muted">请逐项核对后选择批准或拒绝。批准其中一项不会影响其他项。</p>
          </div>
          <span class="badge neutral">{{ runId ? '当前任务' : '全部任务' }}</span>
        </div>

        <div class="approval-scroll">
          <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />

          <section v-for="(item, index) in items" :key="item.approval_item_id" class="approval-item">
            <div class="approval-head">
              <div>
                <span class="approval-index">{{ index + 1 }}</span><h2>{{ item.title }}</h2>
                <p class="muted">{{ actionVersion(item) }}</p>
              </div>
              <span class="badge" :class="item.risk_level === 'sensitive' ? 'sensitive' : 'write'">
                {{ item.risk_level }} · {{ statusText(item.status) }}
              </span>
            </div>

            <dl v-if="previewEntries(item).length" class="approval-params">
              <div v-for="entry in previewEntries(item)" :key="entry.key">
                <dt>{{ entry.label }}</dt><dd>{{ entry.value }}</dd>
              </div>
            </dl>
            <p v-else class="muted">此操作没有额外的可展示参数。</p>

            <div v-if="item.status === 'pending'" class="approval-actions">
              <el-checkbox v-model="checkedById[item.approval_item_id]">
                我已核对该操作的参数、风险级别和目标对象。
              </el-checkbox>
              <div class="approval-buttons">
                <el-button
                  type="primary"
                  :loading="decidingItemId === item.approval_item_id"
                  :disabled="!checkedById[item.approval_item_id]"
                  @click="decide(item, 'approved')"
                >{{ approvalLabel(item) }}</el-button>
                <el-button
                  :loading="decidingItemId === item.approval_item_id"
                  @click="decide(item, 'rejected')"
                >拒绝此项</el-button>
              </div>
            </div>
            <p v-else class="approval-result">
              当前状态：{{ executionByApprovalId[item.approval_item_id]?.message || statusText(item.status) }}
              <span v-if="executionByApprovalId[item.approval_item_id]?.providerResourceId">
                事件 ID：{{ executionByApprovalId[item.approval_item_id].providerResourceId }}
              </span>
            </p>
          </section>

          <div v-if="!loading && items.length === 0 && !error" class="approval-empty">
            <b>没有待确认操作</b>
            <span>日程查询是只读操作；只有 Worker 生成会议草稿后才会出现在这里。</span>
          </div>
        </div>
      </article>
    </section>
  </main>
</template>
