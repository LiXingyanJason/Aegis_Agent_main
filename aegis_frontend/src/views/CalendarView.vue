<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { subscribeRunEvents } from '../api/run'
import {
  getCalendarDraftDetail,
  getCalendarEventDetail,
  listCalendarDrafts,
  listCalendarEvents,
  listCalendars,
} from '../api/calendar'
import { useAuthStore } from '../store/auth'

const timezone = 'Asia/Shanghai'
const auth = useAuthStore()
const router = useRouter()
const mode = ref('month')
const anchor = ref(todayKey())
const sources = ref([])
const sourceStates = ref({})
const sourceLoading = ref(false)
const sourceError = ref('')
const showDrafts = ref(false)
const drafts = ref([])
const draftsLoading = ref(false)
const draftsError = ref('')
const detailVisible = ref(false)
const detailLoading = ref(false)
const detailError = ref('')
const detail = ref(null)
const detailKind = ref('event')
const detailSource = ref('')
let rangeRequestId = 0
let draftRequestId = 0
let detailRequestId = 0
let pageActive = true
const draftStreams = new Map()
const streamRetryTimers = new Map()
const runEventNos = new Map()

function todayKey() {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date())
  const part = (type) => parts.find((item) => item.type === type)?.value
  return `${part('year')}-${part('month')}-${part('day')}`
}

function fromKey(key) {
  return new Date(`${key}T00:00:00Z`)
}

function keyOf(date) {
  return date.toISOString().slice(0, 10)
}

function shiftDays(key, count) {
  const date = fromKey(key)
  date.setUTCDate(date.getUTCDate() + count)
  return keyOf(date)
}

function startOfWeek(key) {
  return shiftDays(key, -((fromKey(key).getUTCDay() + 6) % 7))
}

const visibleRange = computed(() => {
  if (mode.value === 'day') return { start: anchor.value, end: shiftDays(anchor.value, 1) }
  if (mode.value === 'week') {
    const start = startOfWeek(anchor.value)
    return { start, end: shiftDays(start, 7) }
  }
  const first = `${anchor.value.slice(0, 7)}-01`
  const nextMonth = fromKey(first)
  nextMonth.setUTCMonth(nextMonth.getUTCMonth() + 1)
  const start = startOfWeek(first)
  const nextMonthKey = keyOf(nextMonth)
  const finalWeek = startOfWeek(nextMonthKey)
  const end = finalWeek === nextMonthKey ? finalWeek : shiftDays(finalWeek, 7)
  return { start, end }
})

const rangeParams = computed(() => ({
  startAt: `${visibleRange.value.start}T00:00:00+08:00`,
  endAt: `${visibleRange.value.end}T00:00:00+08:00`,
}))

const days = computed(() => {
  const count = Math.round((fromKey(visibleRange.value.end) - fromKey(visibleRange.value.start)) / 86_400_000)
  return Array.from({ length: count }, (_, index) => shiftDays(visibleRange.value.start, index))
})

const rangeTitle = computed(() => {
  if (mode.value === 'month') return `${anchor.value.slice(0, 4)} 年 ${Number(anchor.value.slice(5, 7))} 月`
  if (mode.value === 'day') return formatDay(anchor.value, { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' })
  return `${formatDay(visibleRange.value.start, { month: 'short', day: 'numeric' })} – ${formatDay(shiftDays(visibleRange.value.end, -1), { month: 'short', day: 'numeric', year: 'numeric' })}`
})

const sourceList = computed(() => sources.value.map((source) => ({
  ...source,
  state: sourceStates.value[source.connection_id] ?? {},
})))

function formatDay(key, options = { month: 'short', day: 'numeric' }) {
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'UTC', ...options }).format(fromKey(key))
}

function formatDateTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}

function formatClock(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: timezone, hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}

function errorMessage(cause, fallback) {
  const detail = cause.response?.data?.detail
  if (Array.isArray(detail)) return detail.map((item) => item.msg).filter(Boolean).join('；') || fallback
  return (typeof detail === 'object' ? detail?.message : detail) || cause.message || fallback
}

function move(count) {
  if (mode.value === 'day') anchor.value = shiftDays(anchor.value, count)
  else if (mode.value === 'week') anchor.value = shiftDays(anchor.value, 7 * count)
  else {
    const date = fromKey(`${anchor.value.slice(0, 7)}-01`)
    date.setUTCMonth(date.getUTCMonth() + count)
    anchor.value = keyOf(date)
  }
}

async function loadSources() {
  sourceLoading.value = true
  sourceError.value = ''
  try {
    sources.value = await listCalendars()
  } catch (cause) {
    sources.value = []
    sourceError.value = errorMessage(cause, '加载日历来源失败。')
  } finally {
    sourceLoading.value = false
  }
}

async function loadVisible() {
  const requestId = ++rangeRequestId
  sourceStates.value = Object.fromEntries(sources.value.map((source) => [source.connection_id, { loading: true, events: [] }]))
  const draftsPromise = showDrafts.value ? loadDrafts() : Promise.resolve()
  await Promise.all(sources.value.map(async (source) => {
    try {
      const result = await listCalendarEvents({
        connectionId: source.connection_id,
        calendarId: source.calendar_id,
        ...rangeParams.value,
      })
      if (result.complete === false) throw new Error('服务端未完整返回此范围的日程。')
      if (requestId === rangeRequestId) {
        sourceStates.value[source.connection_id] = {
          loading: false, events: result.events ?? [], fetchedAt: result.fetched_at,
        }
      }
    } catch (cause) {
      if (requestId === rangeRequestId) {
        sourceStates.value[source.connection_id] = {
          loading: false, events: [], error: errorMessage(cause, '读取日程失败。'),
        }
      }
    }
  }))
  await draftsPromise
}

async function loadDrafts() {
  const requestId = ++draftRequestId
  const rangeId = rangeRequestId
  draftsLoading.value = true
  draftsError.value = ''
  drafts.value = []
  try {
    const result = await listCalendarDrafts(rangeParams.value)
    if (requestId === draftRequestId && rangeId === rangeRequestId && showDrafts.value) {
      drafts.value = result
      syncDraftStreams()
    }
  } catch (cause) {
    if (requestId === draftRequestId && rangeId === rangeRequestId && showDrafts.value) {
      draftsError.value = errorMessage(cause, '读取待确认草稿失败。')
      stopAllDraftStreams()
    }
  } finally {
    if (requestId === draftRequestId) draftsLoading.value = false
  }
}

function stopDraftStream(runId) {
  const timer = streamRetryTimers.get(runId)
  if (timer) window.clearTimeout(timer)
  streamRetryTimers.delete(runId)
  draftStreams.get(runId)?.abort()
  draftStreams.delete(runId)
}

function stopAllDraftStreams() {
  for (const runId of new Set([...draftStreams.keys(), ...streamRetryTimers.keys()])) stopDraftStream(runId)
}

function syncDraftStreams() {
  if (!pageActive || !showDrafts.value || document.hidden) {
    stopAllDraftStreams()
    return
  }
  const runIds = new Set(drafts.value.filter((draft) => canConfirm(draft)).map((draft) => draft.run_id))
  for (const runId of new Set([...draftStreams.keys(), ...streamRetryTimers.keys()])) {
    if (!runIds.has(runId)) stopDraftStream(runId)
  }
  for (const runId of runIds) {
    if (!draftStreams.has(runId) && !streamRetryTimers.has(runId)) void watchDraftRun(runId)
  }
}

async function watchDraftRun(runId) {
  const controller = new AbortController()
  draftStreams.set(runId, controller)
  let terminal = false
  try {
    await subscribeRunEvents(runId, {
      lastEventId: runEventNos.get(runId) ?? 0,
      signal: controller.signal,
      onEvent: async (event) => {
        const eventNo = Number(event.data?.event_no ?? event.id)
        if (Number.isSafeInteger(eventNo)) {
          if (eventNo <= (runEventNos.get(runId) ?? 0)) return
          runEventNos.set(runId, eventNo)
        }
        if (['approval_executed', 'run_completed', 'run_failed'].includes(event.event)) {
          terminal = ['run_completed', 'run_failed'].includes(event.event)
          await loadVisible()
        }
      },
    })
  } catch {
    // 流不可用时保留手动刷新；可见页面会尝试重新连接。
  } finally {
    if (draftStreams.get(runId) === controller) draftStreams.delete(runId)
    if (!terminal && !controller.signal.aborted && pageActive && showDrafts.value && !document.hidden
      && drafts.value.some((draft) => draft.run_id === runId && canConfirm(draft))) {
      streamRetryTimers.set(runId, window.setTimeout(() => {
        streamRetryTimers.delete(runId)
        void watchDraftRun(runId)
      }, 5000))
    }
  }
}

function onVisibilityChange() {
  if (document.hidden) stopAllDraftStreams()
  else void loadVisible()
}

async function refresh() {
  ++rangeRequestId
  await loadSources()
  await loadVisible()
}

function itemsForDay(key) {
  const start = new Date(`${key}T00:00:00+08:00`).getTime()
  const end = new Date(`${shiftDays(key, 1)}T00:00:00+08:00`).getTime()
  const overlaps = (item) => {
    const itemStart = new Date(item.start_at).getTime()
    const itemEnd = new Date(item.end_at).getTime()
    return itemStart < end && itemEnd > start
  }
  const events = sourceList.value.flatMap((source) => (source.state.events ?? [])
    .filter(overlaps)
    .map((event) => ({
      ...event, kind: 'event', sourceName: source.name, source: source.source,
      connection_id: source.connection_id, calendar_id: source.calendar_id,
      key: `${source.connection_id}:${source.calendar_id}:${event.external_event_id}`,
    })))
  const pendingDrafts = showDrafts.value ? drafts.value.filter(overlaps).map((draft) => ({
    ...draft, kind: 'draft', key: `draft:${draft.draft_id}`,
  })) : []
  return [...events, ...pendingDrafts].sort((left, right) =>
    new Date(left.start_at) - new Date(right.start_at)
    || new Date(left.end_at) - new Date(right.end_at)
    || left.key.localeCompare(right.key))
}

function isOutsideMonth(key) {
  return key.slice(0, 7) !== anchor.value.slice(0, 7)
}

async function openDetail(item) {
  const requestId = ++detailRequestId
  detailKind.value = item.kind
  detailSource.value = item.source ?? ''
  detail.value = null
  detailError.value = ''
  detailLoading.value = true
  detailVisible.value = true
  try {
    const result = item.kind === 'draft'
      ? await getCalendarDraftDetail(item.draft_id)
      : await getCalendarEventDetail({
        connectionId: item.connection_id,
        calendarId: item.calendar_id,
        externalEventId: item.external_event_id,
      })
    if (requestId === detailRequestId) detail.value = result
  } catch (cause) {
    if (requestId === detailRequestId) detailError.value = errorMessage(cause, '加载详情失败。')
  } finally {
    if (requestId === detailRequestId) detailLoading.value = false
  }
}

function canConfirm(draft) {
  return draft?.approval_status === 'pending' && draft.can_confirm && draft.run_id
    && (!draft.expires_at || new Date(draft.expires_at) > new Date())
}

function draftLabel(draft) {
  if (draft.approval_status === 'pending' && draft.expires_at && new Date(draft.expires_at) <= new Date()) return '确认已过期'
  if (draft.approval_status === 'pending' && !draft.can_confirm) return '暂不可确认'
  return draft.approval_status === 'pending' ? '待确认草稿' : `草稿 · ${draft.approval_status || '未提交'}`
}

function draftStatusText(status) {
  return {
    pending_approval: '等待逐项确认',
    approved_executing: '已批准，正在执行',
    executed: '已创建',
    rejected: '已拒绝',
    failed: '执行失败',
  }[status] ?? status ?? '—'
}

function approvalStatusText(status) {
  return {
    pending: '待确认',
    approved_executing: '已批准，正在执行',
    executed: '已执行',
    rejected: '已拒绝',
    expired: '已过期',
    failed: '执行失败',
  }[status] ?? status ?? '—'
}

function recommendation(draft) {
  return draft?.recommendation ?? null
}

async function signOut() {
  await auth.signOut()
  await router.replace({ name: 'login' })
}

watch([mode, anchor], () => { void loadVisible() })
watch(showDrafts, (visible) => {
  if (visible) void loadDrafts()
  else {
    ++draftRequestId
    stopAllDraftStreams()
    draftsLoading.value = false
    drafts.value = []
    draftsError.value = ''
  }
})
onMounted(() => {
  document.addEventListener('visibilitychange', onVisibilityChange)
  void refresh()
})
onBeforeUnmount(() => {
  pageActive = false
  ++rangeRequestId
  ++draftRequestId
  ++detailRequestId
  stopAllDraftStreams()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <main class="workspace">
    <aside class="sidebar">
      <div class="brand"><span class="mark">⌾</span>Aegis PA</div>
      <p class="nav-label app-nav-label">工作空间</p>
      <router-link class="nav" :to="{ name: 'today' }">▦ 今日工作台</router-link>
      <router-link class="nav" :to="{ name: 'tasks' }">◌ 任务对话</router-link>
      <router-link class="nav active" :to="{ name: 'calendar' }">▦ 日历</router-link>
      <router-link class="nav" :to="{ name: 'mail-management' }">✉ 邮件管理</router-link>
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

    <section class="content calendar-content">
      <header class="page-header calendar-page-header">
        <div>
          <h1>日历</h1>
          <p class="muted">查看正式日程与站内待确认草稿。显示时区：北京时间（Asia/Shanghai）。</p>
        </div>
        <el-button :loading="sourceLoading" @click="refresh">刷新日程</el-button>
      </header>

      <article class="card calendar-card">
        <div class="calendar-toolbar">
          <div class="calendar-navigation">
            <el-button @click="anchor = todayKey()">今天</el-button>
            <el-button aria-label="上一时段" @click="move(-1)">‹</el-button>
            <el-button aria-label="下一时段" @click="move(1)">›</el-button>
            <h2>{{ rangeTitle }}</h2>
          </div>
          <div class="calendar-controls">
            <el-switch v-model="showDrafts" active-text="显示待确认草稿" />
            <el-radio-group v-model="mode" size="small" aria-label="日历视图">
              <el-radio-button value="month">月</el-radio-button>
              <el-radio-button value="week">周</el-radio-button>
              <el-radio-button value="day">日</el-radio-button>
            </el-radio-group>
          </div>
        </div>

        <div class="calendar-sources">
          <span class="calendar-legend"><i class="calendar-legend-dot" />正式日程</span>
          <span class="calendar-legend"><i class="calendar-legend-dot draft" />待确认草稿（尚未占用日历）</span>
          <span v-if="sourceLoading" class="muted">正在读取日历来源…</span>
          <span v-for="source in sourceList" :key="source.connection_id" class="calendar-source" :class="{ failed: source.state.error }">
            {{ source.name }}<small v-if="source.source === 'mock_calendar'">模拟数据</small>
            <small v-if="source.state.loading">加载中</small>
            <small v-else-if="source.state.error">读取失败</small>
            <small v-else>已更新</small>
          </span>
        </div>

        <div class="calendar-notices">
          <el-alert v-if="sourceError" :title="sourceError" type="error" :closable="false" show-icon />
          <el-alert v-else-if="!sourceLoading && sources.length === 0" title="尚未连接具有读取权限的日历" description="连接日历后点击“刷新日程”。待确认草稿仍可单独查看。" type="info" :closable="false" show-icon />
          <el-alert v-for="source in sourceList.filter((item) => item.state.error)" :key="source.connection_id" :title="`${source.name}：${source.state.error}`" type="error" :closable="false" show-icon />
          <el-alert v-if="draftsError" :title="`待确认草稿：${draftsError}`" type="error" :closable="false" show-icon />
        </div>

        <div class="calendar-scroll">
          <div class="calendar-grid" :class="`calendar-grid-${mode}`">
            <div v-if="mode === 'month'" class="calendar-weekdays">
              <span v-for="weekday in ['周一', '周二', '周三', '周四', '周五', '周六', '周日']" :key="weekday">{{ weekday }}</span>
            </div>
            <div class="calendar-days" :class="`calendar-days-${mode}`">
              <section v-for="day in days" :key="day" class="calendar-day" :class="{ 'outside-month': mode === 'month' && isOutsideMonth(day), today: day === todayKey() }">
                <div class="calendar-day-head">
                  <span v-if="mode !== 'month'" class="calendar-day-weekday">{{ formatDay(day, { weekday: 'long' }) }}</span>
                  <b>{{ mode === 'month' ? Number(day.slice(-2)) : formatDay(day) }}</b>
                </div>
                <div class="calendar-day-items">
                  <button v-for="item in itemsForDay(day)" :key="item.key" type="button" class="calendar-event" :class="item.kind" @click="openDetail(item)">
                    <span class="calendar-event-time">{{ formatClock(item.start_at) }}–{{ formatClock(item.end_at) }}</span>
                    <strong>{{ item.title }}</strong>
                    <small>{{ item.kind === 'draft' ? draftLabel(item) : item.sourceName }}<template v-if="item.source === 'mock_calendar'"> · 模拟</template></small>
                  </button>
                  <span v-if="mode !== 'month' && !itemsForDay(day).length" class="calendar-day-empty">暂无日程</span>
                </div>
              </section>
            </div>
          </div>
        </div>
        <div class="calendar-footnote">正式日程由日历服务读取；草稿仅保存在 Aegis，获批并执行成功后才会成为正式日程。<span v-if="draftsLoading">正在读取草稿…</span></div>
      </article>
    </section>

    <el-dialog v-model="detailVisible" :title="detailKind === 'draft' ? '会议草稿详情' : '日程详情'" width="min(620px, calc(100vw - 32px))" class="calendar-detail-dialog">
      <div v-loading="detailLoading" class="calendar-detail-body">
        <el-alert v-if="detailError" :title="detailError" type="error" :closable="false" show-icon />
        <template v-else-if="detail">
          <div class="calendar-detail-heading">
            <h3>{{ detail.title }}</h3>
            <span class="badge" :class="detailKind === 'draft' ? 'write' : 'read'">{{ detailKind === 'draft' ? draftLabel(detail) : detailSource === 'mock_calendar' ? '模拟日程' : '正式日程' }}</span>
          </div>
          <p v-if="detailKind === 'draft'" class="calendar-draft-warning">此草稿尚未占用日历。只有逐项批准且服务端执行成功后，才会创建正式日程。</p>
          <dl class="calendar-detail-fields">
            <div><dt>开始</dt><dd>{{ formatDateTime(detail.start_at) }}</dd></div>
            <div><dt>结束</dt><dd>{{ formatDateTime(detail.end_at) }}</dd></div>
            <div v-if="detail.location"><dt>地点</dt><dd>{{ detail.location }}</dd></div>
            <div v-if="detail.attendees?.length"><dt>参与者</dt><dd>{{ detail.attendees.join('、') }}</dd></div>
            <div v-if="detail.description"><dt>说明</dt><dd class="calendar-description">{{ detail.description }}</dd></div>
            <div v-if="detailKind === 'draft'"><dt>草稿状态</dt><dd>{{ draftStatusText(detail.draft_status) }}</dd></div>
            <div v-if="detailKind === 'draft'"><dt>确认状态</dt><dd>{{ approvalStatusText(detail.approval_status) }}</dd></div>
            <div v-if="detailKind === 'draft' && detail.expires_at"><dt>确认截止</dt><dd>{{ formatDateTime(detail.expires_at) }}</dd></div>
          </dl>
          <section v-if="detailKind === 'draft' && recommendation(detail)" class="calendar-recommendation">
            <b>推荐原因</b>
            <p v-if="recommendation(detail).reason">{{ recommendation(detail).reason }}</p>
            <template v-if="recommendation(detail).alternative_slots?.length">
              <b>备选时间</b>
              <p v-for="slot in recommendation(detail).alternative_slots" :key="slot.slot_id || `${slot.start_at}-${slot.end_at}`">
                {{ formatDateTime(slot.start_at) }} – {{ formatDateTime(slot.end_at) }}
              </p>
            </template>
          </section>
        </template>
      </div>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
        <router-link v-if="detailKind === 'draft' && canConfirm(detail)" class="calendar-confirm-link" :to="{ name: 'confirmations', query: { run_id: detail.run_id } }">进入逐项确认</router-link>
      </template>
    </el-dialog>
  </main>
</template>
