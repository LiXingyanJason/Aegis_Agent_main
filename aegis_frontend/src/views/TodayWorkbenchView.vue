<script setup>
import AppNavigation from '../components/AppNavigation.vue'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { listCalendars, listCalendarEvents } from '../api/calendar'
import { listTodos } from '../api/todo'
import { listMailMessages, listSentMailMessages } from '../api/mail'
import { useAuthStore } from '../store/auth'

const auth = useAuthStore()
const router = useRouter()
const now = ref(new Date())
const meetings = ref([])
const todos = ref([])
const importantMails = ref([])
const waitingReplies = ref([])
const status = ref({ meetings: 'loading', todos: 'loading', mail: 'loading', replies: 'loading' })
const errors = ref({ meetings: '', todos: '', mail: '', replies: '' })
const refreshing = ref(false)

const today = computed(() => new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: 'long', day: 'numeric', weekday: 'long',
}).format(now.value))
const mailCount = computed(() => {
  if (['loading', 'error'].includes(status.value.mail) || ['loading', 'error'].includes(status.value.replies)) return '—'
  return importantMails.value.length + waitingReplies.value.length
})

function dayKey(date) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(date)
  const part = (name) => parts.find((item) => item.type === name).value
  return `${part('year')}-${part('month')}-${part('day')}`
}

function todayRange() {
  const start = dayKey(now.value)
  const [year, month, day] = start.split('-').map(Number)
  const end = new Date(Date.UTC(year, month - 1, day + 1)).toISOString().slice(0, 10)
  return { startAt: `${start}T00:00:00+08:00`, endAt: `${end}T00:00:00+08:00` }
}

function formatTime(value, options = {}) {
  if (!value) return '未设置时间'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '时间未知'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
    ...options,
  }).format(date)
}

function timeOnly(value) {
  return formatTime(value, { month: undefined, day: undefined })
}

function detailMessage(cause, fallback) {
  const detail = cause?.response?.data?.detail
  return (typeof detail === 'object' ? detail?.message : detail) || cause?.message || fallback
}

function setState(section, value, error = '') {
  status.value = { ...status.value, [section]: value }
  errors.value = { ...errors.value, [section]: error }
}

async function loadMeetings() {
  setState('meetings', 'loading')
  meetings.value = []
  try {
    const calendars = await listCalendars()
    if (!calendars.length) {
      setState('meetings', 'missing', '尚未连接具有读取权限的日历。')
      return
    }
    const range = todayRange()
    const outcomes = await Promise.allSettled(calendars.map((calendar) => listCalendarEvents({
      connectionId: calendar.connection_id, calendarId: calendar.calendar_id,
      ...range, timezone: 'Asia/Shanghai',
    })))
    const succeeded = outcomes.filter((item) => item.status === 'fulfilled')
    if (!succeeded.length) throw outcomes[0].reason
    meetings.value = succeeded.flatMap((item) => item.value.events.map((event) => ({
      ...event, connection_id: item.value.connection_id,
      calendar_id: item.value.calendar_id,
      source: item.value.source,
    }))).sort((a, b) => new Date(a.start_at) - new Date(b.start_at))
    const failed = outcomes.length - succeeded.length
    setState('meetings', failed ? 'partial' : 'ready', failed ? `${failed} 个日历加载失败，当前只显示已成功加载的日程。` : '')
  } catch (cause) {
    setState('meetings', 'error', detailMessage(cause, '加载今日日程失败。'))
  }
}

async function loadTodos() {
  setState('todos', 'loading')
  todos.value = []
  try {
    const items = await listTodos('draft')
    todos.value = items.sort((a, b) => {
      if (!a.due_at) return 1
      if (!b.due_at) return -1
      return new Date(a.due_at) - new Date(b.due_at)
    })
    setState('todos', 'ready')
  } catch (cause) {
    setState('todos', 'error', detailMessage(cause, '加载待办失败。'))
  }
}

async function loadImportantMail() {
  setState('mail', 'loading')
  importantMails.value = []
  try {
    const result = await listMailMessages()
    importantMails.value = (result.items ?? []).filter((item) => item.is_important)
    setState('mail', 'ready')
  } catch (cause) {
    setState('mail', 'error', detailMessage(cause, '加载重要邮件失败。'))
  }
}

async function loadWaitingReplies() {
  setState('replies', 'loading')
  waitingReplies.value = []
  try {
    const items = await listSentMailMessages()
    waitingReplies.value = items.filter((item) => item.status === 'sent' && item.awaiting_reply)
    setState('replies', 'ready')
  } catch (cause) {
    setState('replies', 'error', detailMessage(cause, '加载等待回复事项失败。'))
  }
}

function countFor(section, items) {
  if (['loading', 'error', 'missing'].includes(status.value[section])) return '—'
  return items.length
}

async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  now.value = new Date()
  await Promise.all([loadMeetings(), loadTodos(), loadImportantMail(), loadWaitingReplies()])
  refreshing.value = false
}

async function signOut() {
  await auth.signOut()
  await router.replace({ name: 'login' })
}

onMounted(refresh)
</script>

<template>
  <main class="workspace">
    <aside class="sidebar">
      <div class="brand">Aegis PA</div>
      <AppNavigation active="today" />

      <div class="identity">
        <b>{{ auth.profile?.name }}</b>
        <span>{{ auth.profile?.email || '已通过 Keycloak 登录' }}</span>
        <el-button text type="primary" @click="signOut">退出登录</el-button>
      </div>
    </aside>

    <section class="content today-content">
      <div class="today-scroll">
        <header class="page-header today-header">
          <div>
            <span class="today-date">{{ today }}</span>
            <h1>今日工作台</h1>
            <p class="muted">集中查看日程、待办和需要跟进的邮件，让今天的工作更清晰。</p>
          </div>
          <div class="today-header-actions">
            <el-button :loading="refreshing" @click="refresh">刷新</el-button>
            <router-link class="today-new-task" :to="{ name: 'tasks' }">前往任务对话 <span aria-hidden="true">→</span></router-link>
          </div>
        </header>

        <div class="today-demo-notice" role="note">
          <span class="today-demo-icon" aria-hidden="true">i</span>
          <span>数据来自当前账号可访问的日历、站内待办和邮件。重要邮件、等待回复由你手动标记；不会根据主题或邮件正文自动猜测。</span>
        </div>

        <div class="today-summary" aria-label="今日概览">
          <router-link class="today-summary-card card" :to="{ name: 'calendar' }">
            <span class="summary-icon meeting" aria-hidden="true">▦</span>
            <span class="summary-copy"><small>今日会议与日程</small><strong>{{ countFor('meetings', meetings) }}</strong><span>查看日历 →</span></span>
          </router-link>
          <router-link class="today-summary-card card" :to="{ name: 'todo-plans' }">
            <span class="summary-icon todo" aria-hidden="true">☷</span>
            <span class="summary-copy"><small>待办事项</small><strong>{{ countFor('todos', todos) }}</strong><span>查看待办 →</span></span>
          </router-link>
          <router-link class="today-summary-card card" :to="{ name: 'mail-management' }">
            <span class="summary-icon mail" aria-hidden="true">✉</span>
            <span class="summary-copy"><small>邮件</small><strong>{{ mailCount }}</strong><span>重要邮件 + 等待回复 →</span></span>
          </router-link>
        </div>

        <div class="today-sections">
          <section class="card today-panel" aria-labelledby="today-meetings-title">
            <div class="today-panel-head">
              <div><h2 id="today-meetings-title">今日会议与日程</h2><p>按北京时间查看已连接日历的正式安排</p></div>
              <router-link :to="{ name: 'calendar' }">查看日历 →</router-link>
            </div>
            <div class="today-panel-list meeting-list">
              <p v-if="errors.meetings" class="today-section-message" :class="{ error: status.meetings !== 'partial' }">{{ errors.meetings }}</p>
              <p v-if="status.meetings === 'loading'" class="today-section-message">正在加载日历…</p>
              <p v-else-if="status.meetings === 'ready' && !meetings.length" class="today-section-message">今天没有日程。</p>
              <article v-for="meeting in meetings.slice(0, 5)" :key="`${meeting.connection_id}:${meeting.calendar_id}:${meeting.external_event_id}`" class="today-meeting">
                <time>{{ timeOnly(meeting.start_at) }}–{{ timeOnly(meeting.end_at) }}</time>
                <div><h3>{{ meeting.title }}</h3><p>{{ meeting.attendee_count }} 位参会人 · {{ meeting.source === 'mock_calendar' ? '模拟日历' : '正式日历' }}</p></div>
                <span class="today-pill blue">日程</span>
              </article>
              <p v-if="meetings.length > 5" class="today-section-message">另外 {{ meetings.length - 5 }} 项请到日历页查看。</p>
            </div>
          </section>

          <section class="card today-panel" aria-labelledby="today-todos-title">
            <div class="today-panel-head">
              <div><h2 id="today-todos-title">待办事项</h2><p>当前未完成，按截止时间排序</p></div>
              <router-link :to="{ name: 'todo-plans' }">查看待办 →</router-link>
            </div>
            <div class="today-panel-list">
              <p v-if="errors.todos" class="today-section-message error">{{ errors.todos }}</p>
              <p v-if="status.todos === 'loading'" class="today-section-message">正在加载待办…</p>
              <p v-else-if="status.todos === 'ready' && !todos.length" class="today-section-message">暂无待处理事项。</p>
              <article v-for="todo in todos.slice(0, 5)" :key="todo.todo_id" class="today-row">
                <span class="today-row-marker todo-marker" aria-hidden="true">○</span>
                <div><h3>{{ todo.content }}</h3><p>{{ todo.due_at ? `截止 ${formatTime(todo.due_at)}` : '未设置截止时间' }}</p></div>
                <span class="today-pill neutral">待处理</span>
              </article>
              <p v-if="todos.length > 5" class="today-section-message">另外 {{ todos.length - 5 }} 项请到待办页查看。</p>
            </div>
          </section>

          <section class="card today-panel today-mail-panel" aria-labelledby="today-mails-title">
            <div class="today-panel-head">
              <div><h2 id="today-mails-title">邮件</h2><p>重要邮件与等待回复，{{ mailCount === '—' ? '数量暂不可用' : `共 ${mailCount} 封` }}</p></div>
              <router-link :to="{ name: 'mail-management' }">查看邮件 →</router-link>
            </div>
            <div class="today-mail-groups">
              <div class="today-mail-group">
                <div class="today-mail-group-head"><h3>重要邮件</h3><router-link :to="{ name: 'mail-management' }">收到 →</router-link></div>
                <div class="today-panel-list">
                  <p v-if="errors.mail" class="today-section-message error">{{ errors.mail }}</p>
                  <p v-if="status.mail === 'loading'" class="today-section-message">正在加载邮件…</p>
                  <p v-else-if="status.mail === 'ready' && !importantMails.length" class="today-section-message">暂无标记为重要的邮件。</p>
                  <article v-for="mail in importantMails.slice(0, 5)" :key="mail.email_id" class="today-row">
                    <span class="today-row-marker mail-marker" aria-hidden="true">✉</span>
                    <div><h3>{{ mail.subject }}</h3><p>{{ mail.customer || mail.from_email }} · {{ formatTime(mail.received_at) }}</p></div>
                    <span class="today-pill amber">重要</span>
                  </article>
                  <p v-if="importantMails.length > 5" class="today-section-message">另外 {{ importantMails.length - 5 }} 封请到邮件页查看。</p>
                </div>
              </div>
              <div class="today-mail-group">
                <div class="today-mail-group-head"><h3>等待回复</h3><router-link :to="{ name: 'mail-management', query: { tab: 'sent' } }">已发送 →</router-link></div>
                <div class="today-panel-list">
                  <p v-if="errors.replies" class="today-section-message error">{{ errors.replies }}</p>
                  <p v-if="status.replies === 'loading'" class="today-section-message">正在加载已发送邮件…</p>
                  <p v-else-if="status.replies === 'ready' && !waitingReplies.length" class="today-section-message">暂无标记为等待回复的邮件。</p>
                  <article v-for="reply in waitingReplies.slice(0, 5)" :key="reply.sent_message_id" class="today-row">
                    <span class="today-row-marker reply-marker" aria-hidden="true">↪</span>
                    <div><h3>{{ reply.subject }}</h3><p>已发送 · {{ formatTime(reply.sent_at) }}</p></div>
                    <span class="today-pill purple">待回复</span>
                  </article>
                  <p v-if="waitingReplies.length > 5" class="today-section-message">另外 {{ waitingReplies.length - 5 }} 项请到已发送分区查看。</p>
                </div>
              </div>
            </div>
          </section>
        </div>
      </div>
    </section>
  </main>
</template>

<style scoped>
.today-content { min-width: 0; padding-top: 0; padding-bottom: 0; }
.today-scroll { min-height: 0; flex: 1; overflow-y: auto; padding: 34px 2px 40px; }
.today-header { align-items: center; margin-bottom: 20px; }
.today-header-actions { display: flex; align-items: center; gap: 10px; }
.today-header h1 { margin: 5px 0 7px; }
.today-header p { margin: 0; }
.today-date { color: #526176; font-size: 13px; font-weight: 700; }
.today-new-task { display: inline-flex; align-items: center; gap: 12px; flex: none; padding: 10px 15px; border-radius: 8px; background: #266dcc; color: #fff; font-weight: 700; text-decoration: none; }
.today-new-task:hover { background: #1858af; }
.today-demo-notice { display: flex; align-items: center; gap: 10px; padding: 11px 14px; border: 1px solid #c9dcf8; border-radius: 9px; background: #f2f7ff; color: #355679; font-size: 12px; line-height: 1.5; }
.today-demo-icon { display: grid; width: 18px; height: 18px; flex: none; place-items: center; border-radius: 50%; background: #266dcc; color: #fff; font-family: Georgia, serif; font-weight: 700; }
.today-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin: 20px 0; }
.today-summary-card { display: flex; min-width: 0; align-items: flex-start; gap: 13px; padding: 19px; color: inherit; text-decoration: none; transition: border-color .15s, transform .15s; }
.today-summary-card:hover { border-color: #a6c9f3; transform: translateY(-2px); }
.summary-icon { display: grid; width: 38px; height: 38px; flex: none; place-items: center; border-radius: 10px; font-size: 21px; }
.summary-icon.meeting { background: #eaf3ff; color: #266dcc; }
.summary-icon.todo { background: #e9f7f2; color: #17805f; }
.summary-icon.mail { background: #fff3e5; color: #b6661b; }
.summary-copy { display: grid; gap: 2px; }
.summary-copy small { color: #667085; font-size: 12px; }
.summary-copy strong { color: #1f354d; font-size: 27px; line-height: 1.2; }
.summary-copy > span { color: #266dcc; font-size: 11px; font-weight: 700; }
.today-sections { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }
.today-panel { min-width: 0; overflow: hidden; }
.today-mail-panel { grid-column: 1 / -1; }
.today-mail-groups { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.today-mail-group { min-width: 0; }
.today-mail-group + .today-mail-group { border-left: 1px solid #e4e9f0; }
.today-mail-group-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 15px 20px 0; }
.today-mail-group-head h3 { margin: 0; color: #243952; font-size: 13px; }
.today-mail-group-head a { color: #266dcc; font-size: 12px; text-decoration: none; }
.today-mail-group-head a:hover { text-decoration: underline; }
.today-panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 18px 20px; border-bottom: 1px solid #e4e9f0; }
.today-panel-head h2 { margin: 0 0 4px; color: #1f354d; font-size: 17px; }
.today-panel-head p { margin: 0; color: #667085; font-size: 12px; }
.today-panel-head a { flex: none; color: #266dcc; font-size: 12px; font-weight: 700; text-decoration: none; }
.today-panel-head a:hover { text-decoration: underline; }
.today-panel-list { padding: 0 20px; }
.today-meeting, .today-row { display: flex; min-width: 0; align-items: center; gap: 12px; padding: 17px 0; border-bottom: 1px solid #edf0f5; }
.today-meeting:last-child, .today-row:last-child { border-bottom: 0; }
.today-meeting time { min-width: 90px; align-self: flex-start; padding-top: 2px; color: #266dcc; font-size: 12px; font-weight: 800; white-space: nowrap; }
.today-meeting > div, .today-row > div { min-width: 0; flex: 1; }
.today-meeting h3, .today-row h3 { margin: 0 0 5px; overflow-wrap: anywhere; color: #243952; font-size: 13px; }
.today-meeting p, .today-row p { margin: 0; overflow-wrap: anywhere; color: #667085; font-size: 11px; line-height: 1.5; }
.today-section-message { margin: 0; padding: 15px 0; color: #667085; font-size: 12px; line-height: 1.5; }
.today-section-message.error { color: #b42318; }
.today-row-marker { display: grid; width: 28px; height: 28px; flex: none; place-items: center; border-radius: 8px; font-size: 16px; }
.todo-marker { background: #e9f7f2; color: #17805f; }
.mail-marker { background: #fff3e5; color: #b6661b; }
.reply-marker { background: #f0edff; color: #7566c2; }
.today-pill { flex: none; padding: 5px 8px; border-radius: 20px; font-size: 11px; font-weight: 700; white-space: nowrap; }
.today-pill.blue { background: #eaf3ff; color: #266dcc; }
.today-pill.amber { background: #fff3e5; color: #a55c12; }
.today-pill.neutral { background: #f1f4f8; color: #526176; }
.today-pill.purple { background: #f0edff; color: #6554b2; }
@media (max-width: 1150px) { .today-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 900px) { .today-sections, .today-mail-groups { grid-template-columns: 1fr; }.today-mail-group + .today-mail-group { border-left: 0; border-top: 1px solid #e4e9f0; } }
@media (max-width: 800px) { .today-content { padding-top: 0; padding-bottom: 0; }.today-scroll { padding-top: 24px; }.today-header { align-items: flex-start; flex-direction: column; }.today-summary { gap: 10px; }.today-summary-card { padding: 14px; } }
@media (max-width: 480px) { .today-summary { grid-template-columns: 1fr; }.summary-icon { width: 30px; height: 30px; }.today-meeting { flex-wrap: wrap; }.today-meeting time { width: 100%; }.today-pill { font-size: 10px; } }
</style>
