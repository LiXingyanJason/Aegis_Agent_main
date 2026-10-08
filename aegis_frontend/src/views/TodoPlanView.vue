<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getTodo, listTodos, updateTodo } from '../api/todo'
import { useAuthStore } from '../store/auth'

const auth = useAuthStore()
const router = useRouter()
const activeStatus = ref('')
const todos = ref([])
const loading = ref(false)
const error = ref('')
const editorVisible = ref(false)
const detailLoading = ref(false)
const saving = ref(false)
const editorError = ref('')
const todoForm = ref(null)

const pageTitle = computed(() => ({
  '': '全部待办',
  draft: '待处理',
  completed: '已完成',
  discarded: '已丢弃',
}[activeStatus.value]))

function formatTime(value) {
  if (!value) return '未设置'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(new Date(value))
}

function formatDateTimeInput(value) {
  if (!value) return ''
  const date = new Date(value)
  const offset = date.getTimezoneOffset()
  return new Date(date.getTime() - offset * 60_000).toISOString().slice(0, 16)
}

function detailMessage(cause, fallback) {
  const detail = cause.response?.data?.detail
  return typeof detail === 'object' ? detail.message : detail || cause.message || fallback
}

async function loadTodos() {
  loading.value = true
  error.value = ''
  try {
    todos.value = await listTodos(activeStatus.value || undefined)
  } catch (cause) {
    error.value = detailMessage(cause, '加载待办计划失败。')
  } finally {
    loading.value = false
  }
}

async function selectStatus(status) {
  activeStatus.value = status
  await loadTodos()
}

async function openTodo(todoId) {
  editorVisible.value = true
  detailLoading.value = true
  editorError.value = ''
  todoForm.value = null
  try {
    const todo = await getTodo(todoId)
    todoForm.value = { ...todo, dueAtInput: formatDateTimeInput(todo.due_at) }
  } catch (cause) {
    editorError.value = detailMessage(cause, '加载待办详情失败。')
  } finally {
    detailLoading.value = false
  }
}

async function saveTodo(payload) {
  if (!todoForm.value) return
  saving.value = true
  editorError.value = ''
  try {
    const updated = await updateTodo(todoForm.value.todo_id, payload)
    todoForm.value = { ...updated, dueAtInput: formatDateTimeInput(updated.due_at) }
    await loadTodos()
  } catch (cause) {
    editorError.value = detailMessage(cause, '保存待办失败。')
  } finally {
    saving.value = false
  }
}

function saveContent() {
  return saveTodo({
    content: todoForm.value.content,
    due_at: todoForm.value.dueAtInput ? new Date(todoForm.value.dueAtInput).toISOString() : null,
  })
}

function setStatus(status) {
  return saveTodo({ status })
}

async function completeTodo(todoId) {
  saving.value = true
  error.value = ''
  try {
    await updateTodo(todoId, { status: 'completed' })
    await loadTodos()
  } catch (cause) {
    error.value = detailMessage(cause, '更新待办状态失败。')
  } finally {
    saving.value = false
  }
}

async function signOut() {
  await auth.signOut()
  await router.replace({ name: 'login' })
}

onMounted(loadTodos)
</script>

<template>
  <main class="workspace">
    <aside class="sidebar">
      <div class="brand"><span class="mark">⌾</span>Aegis PA</div>
      <p class="nav-label app-nav-label">工作空间</p>
      <router-link class="nav" :to="{ name: 'tasks' }">◌ 任务对话</router-link>
      <router-link class="nav" :to="{ name: 'mail-management' }">✉ 邮件管理</router-link>
      <router-link class="nav active" :to="{ name: 'todo-plans' }">☷ 待办计划</router-link>
      <router-link class="nav" :to="{ name: 'confirmations' }">✓ 操作确认</router-link>
      <router-link class="nav" :to="{ name: 'memories' }">◇ 长期记忆</router-link>
      <span class="nav disabled">◫ 连接与审计（待接入）</span>

      <div class="identity">
        <b>{{ auth.profile?.name }}</b>
        <span>{{ auth.profile?.email || '已通过 Keycloak 登录' }}</span>
        <el-button text type="primary" @click="signOut">退出登录</el-button>
      </div>
    </aside>

    <section class="content todo-content">
      <header class="page-header">
        <div>
          <h1>待办计划</h1>
          <p class="muted">管理由邮件摘要生成的站内待办；所有修改只保存于 Aegis，不会创建日历事件或第三方任务。</p>
        </div>
        <span class="badge neutral">{{ todos.length }} 项</span>
      </header>

      <article v-loading="loading" class="card todo-card">
        <div class="card-head">
          <div>
            <h2>{{ pageTitle }}</h2>
            <p class="muted">可编辑待办内容和截止时间，并独立标记完成、恢复或丢弃。</p>
          </div>
          <el-button :loading="loading" @click="loadTodos">刷新</el-button>
        </div>

        <div class="todo-card-body">
          <div class="todo-tabs" role="tablist" aria-label="待办状态">
            <button class="todo-tab" :class="{ active: activeStatus === '' }" type="button" @click="selectStatus('')">全部</button>
            <button class="todo-tab" :class="{ active: activeStatus === 'draft' }" type="button" @click="selectStatus('draft')">待处理</button>
            <button class="todo-tab" :class="{ active: activeStatus === 'completed' }" type="button" @click="selectStatus('completed')">已完成</button>
            <button class="todo-tab" :class="{ active: activeStatus === 'discarded' }" type="button" @click="selectStatus('discarded')">已丢弃</button>
          </div>

          <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
          <div v-else class="todo-list">
            <article v-for="todo in todos" :key="todo.todo_id" class="todo-item" :class="'status-' + todo.status">
              <div class="todo-item-main">
                <div class="todo-item-title">
                  <h3>{{ todo.content }}</h3>
                  <span class="badge" :class="todo.status === 'completed' ? 'success' : 'neutral'">{{ todo.status === 'draft' ? '待处理' : todo.status === 'completed' ? '已完成' : '已丢弃' }}</span>
                </div>
                <p>截止时间：<b :class="{ overdue: todo.status === 'draft' && todo.due_at && new Date(todo.due_at) < new Date() }">{{ formatTime(todo.due_at) }}</b></p>
                <p>来源邮件：{{ todo.source_subject }} · {{ todo.source_sender }}</p>
                <p v-if="todo.completed_at">完成于：{{ formatTime(todo.completed_at) }}</p>
              </div>
              <div class="todo-item-actions">
                <el-button size="small" @click="openTodo(todo.todo_id)">查看 / 编辑</el-button>
                <el-button v-if="todo.status === 'draft'" size="small" type="success" :loading="saving" @click="completeTodo(todo.todo_id)">标记完成</el-button>
              </div>
            </article>
            <div v-if="!loading && todos.length === 0" class="todo-empty">
              <b>{{ activeStatus === 'discarded' ? '暂无已丢弃待办' : '暂无待办计划' }}</b>
              <span>从邮件摘要中选择待办候选并加入草稿后，会在这里显示。</span>
            </div>
          </div>
        </div>
      </article>
    </section>

    <el-dialog v-model="editorVisible" title="待办详情" width="min(680px, calc(100vw - 32px))" destroy-on-close>
      <div v-loading="detailLoading">
        <el-alert v-if="editorError" :title="editorError" type="error" :closable="false" show-icon />
        <template v-else-if="todoForm">
          <p class="todo-source">来源：{{ todoForm.source_subject }} · {{ todoForm.source_sender }}</p>
          <el-form label-position="top">
            <el-form-item label="待办内容">
              <el-input v-model="todoForm.content" type="textarea" :rows="3" :disabled="todoForm.status === 'discarded'" />
            </el-form-item>
            <el-form-item label="截止时间">
              <el-input v-model="todoForm.dueAtInput" type="datetime-local" :disabled="todoForm.status === 'discarded'" />
            </el-form-item>
          </el-form>
          <p class="muted">状态：{{ todoForm.status === 'draft' ? '待处理' : todoForm.status === 'completed' ? '已完成' : '已丢弃' }}</p>
        </template>
      </div>
      <template #footer>
        <el-button @click="editorVisible = false">关闭</el-button>
        <template v-if="todoForm?.status !== 'discarded'">
          <el-button :loading="saving" @click="saveContent">保存修改</el-button>
          <el-button v-if="todoForm?.status === 'draft'" type="success" :loading="saving" @click="setStatus('completed')">标记完成</el-button>
          <el-button v-if="todoForm?.status === 'completed'" :loading="saving" @click="setStatus('draft')">恢复待处理</el-button>
          <el-button type="danger" plain :loading="saving" @click="setStatus('discarded')">丢弃</el-button>
        </template>
        <el-button v-else :loading="saving" @click="setStatus('draft')">恢复待处理</el-button>
      </template>
    </el-dialog>
  </main>
</template>
