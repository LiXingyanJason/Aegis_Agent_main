<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  confirmMemory,
  createMemory,
  deleteMemory,
  getMemory,
  listMemories,
  requestMemoryDeletion,
  updateMemory,
} from '../api/memory'
import { useAuthStore } from '../store/auth'

const auth = useAuthStore()
const router = useRouter()
const query = ref('')
const memories = ref([])
const nextCursor = ref(null)
const loading = ref(false)
const loadingMore = ref(false)
const error = ref('')
const editorVisible = ref(false)
const detailLoading = ref(false)
const saving = ref(false)
const editorError = ref('')
const editing = ref(null)
const deleteVisible = ref(false)
const deleteLoading = ref(false)
const deleteError = ref('')
const deletionToken = ref('')
const deletionExpiresAt = ref(null)
const memoryForm = ref(emptyMemoryForm())

const canLoadMore = computed(() => Boolean(nextCursor.value))

function emptyMemoryForm() {
  return { content: '', isSensitive: false, sourceType: 'user_input', sourceRunId: '' }
}

function formatTime(value) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(new Date(value))
}

function sourceText(source) {
  return source === 'user_confirmed' ? '用户确认' : '用户输入'
}

function detailMessage(cause, fallback) {
  const detail = cause.response?.data?.detail
  return typeof detail === 'object' ? detail.message : detail || cause.message || fallback
}

async function loadMemories({ append = false } = {}) {
  const targetLoading = append ? loadingMore : loading
  targetLoading.value = true
  error.value = ''
  try {
    const result = await listMemories({
      q: query.value.trim() || undefined,
      cursor: append ? nextCursor.value : undefined,
    })
    memories.value = append ? [...memories.value, ...result.items] : result.items
    nextCursor.value = result.next_cursor
  } catch (cause) {
    error.value = detailMessage(cause, '加载长期记忆失败。')
  } finally {
    targetLoading.value = false
  }
}

function searchMemories() {
  nextCursor.value = null
  void loadMemories()
}

function openNewMemory() {
  editing.value = null
  editorError.value = ''
  memoryForm.value = emptyMemoryForm()
  editorVisible.value = true
}

async function openEditMemory(memoryId) {
  editorVisible.value = true
  detailLoading.value = true
  editorError.value = ''
  editing.value = null
  try {
    const memory = await getMemory(memoryId)
    editing.value = memory
    memoryForm.value = {
      content: memory.content,
      isSensitive: memory.is_sensitive,
      sourceType: memory.source,
      sourceRunId: memory.source_run_id || '',
    }
  } catch (cause) {
    editorError.value = detailMessage(cause, '加载记忆详情失败。')
  } finally {
    detailLoading.value = false
  }
}

async function saveMemory() {
  const content = memoryForm.value.content.trim()
  if (!content) return
  saving.value = true
  editorError.value = ''
  try {
    if (editing.value) {
      await updateMemory(editing.value.memory_id, {
        content,
        is_sensitive: memoryForm.value.isSensitive,
      })
    } else if (memoryForm.value.sourceType === 'user_confirmed') {
      await confirmMemory({
        content,
        is_sensitive: memoryForm.value.isSensitive,
        source_run_id: memoryForm.value.sourceRunId,
      })
    } else {
      await createMemory({ content, is_sensitive: memoryForm.value.isSensitive })
    }
    editorVisible.value = false
    await loadMemories()
  } catch (cause) {
    editorError.value = detailMessage(cause, '保存长期记忆失败。')
  } finally {
    saving.value = false
  }
}

async function requestDelete(memoryId) {
  deleteError.value = ''
  deleteLoading.value = true
  deletionToken.value = ''
  deletionExpiresAt.value = null
  try {
    const result = await requestMemoryDeletion(memoryId)
    editing.value = { memory_id: memoryId }
    deletionToken.value = result.deletion_token
    deletionExpiresAt.value = result.expires_at
    deleteVisible.value = true
  } catch (cause) {
    error.value = detailMessage(cause, '创建删除确认失败。')
  } finally {
    deleteLoading.value = false
  }
}

async function confirmDelete() {
  if (!editing.value || !deletionToken.value) return
  deleteLoading.value = true
  deleteError.value = ''
  try {
    await deleteMemory(editing.value.memory_id, deletionToken.value)
    deleteVisible.value = false
    deletionToken.value = ''
    await loadMemories()
  } catch (cause) {
    deleteError.value = detailMessage(cause, '删除确认已失效，请重新发起删除。')
  } finally {
    deleteLoading.value = false
  }
}

async function signOut() {
  await auth.signOut()
  await router.replace({ name: 'login' })
}

onMounted(() => { void loadMemories() })
</script>

<template>
  <main class="workspace">
    <aside class="sidebar">
      <div class="brand"><span class="mark">⌾</span>Aegis PA</div>
      <p class="nav-label app-nav-label">工作空间</p>
      <router-link class="nav" :to="{ name: 'tasks' }">◌ 任务对话</router-link>
      <router-link class="nav" :to="{ name: 'mail-management' }">✉ 邮件管理</router-link>
      <router-link class="nav" :to="{ name: 'todo-plans' }">☷ 待办计划</router-link>
      <router-link class="nav" :to="{ name: 'confirmations' }">✓ 操作确认</router-link>
      <router-link class="nav active" :to="{ name: 'memories' }">◇ 长期记忆</router-link>
      <span class="nav disabled">◫ 连接与审计（待接入）</span>
      <div class="identity">
        <b>{{ auth.profile?.name }}</b>
        <span>{{ auth.profile?.email || '已通过 Keycloak 登录' }}</span>
        <el-button text type="primary" @click="signOut">退出登录</el-button>
      </div>
    </aside>

    <section class="content memory-content">
      <header class="page-header">
        <div>
          <h1>长期记忆</h1>
          <p class="muted">只保存你手动输入或明确确认的偏好。敏感记忆在列表中脱敏，必须主动打开后才会读取正文。</p>
        </div>
        <el-button type="primary" @click="openNewMemory">新增偏好</el-button>
      </header>

      <article v-loading="loading" class="card memory-card">
        <div class="card-head memory-toolbar">
          <div class="memory-search">
            <el-input v-model="query" placeholder="搜索已保存的偏好" clearable @keyup.enter="searchMemories" @clear="searchMemories" />
            <el-button @click="searchMemories">搜索</el-button>
            <span class="badge neutral">已加载 {{ memories.length }} 条</span>
          </div>
          <el-button size="small" :loading="loading" @click="loadMemories">刷新</el-button>
        </div>

        <div class="memory-table-wrap">
          <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
          <table v-else class="memory-table">
            <thead><tr><th>偏好内容</th><th>来源</th><th>更新时间</th><th>敏感性</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="memory in memories" :key="memory.memory_id">
                <td>
                  <div class="title">{{ memory.content_preview }}</div>
                  <div v-if="memory.is_sensitive" class="detail">敏感正文不会随列表返回，点击“查看 / 编辑”后按需读取。</div>
                </td>
                <td><span class="badge read">{{ sourceText(memory.source) }}</span></td>
                <td>{{ formatTime(memory.updated_at) }}</td>
                <td><span class="badge" :class="memory.is_sensitive ? 'sensitive' : 'neutral'">{{ memory.is_sensitive ? '敏感' : '普通' }}</span></td>
                <td class="memory-actions">
                  <el-button link type="primary" @click="openEditMemory(memory.memory_id)">查看 / 编辑</el-button>
                  <el-button link type="danger" :loading="deleteLoading" @click="requestDelete(memory.memory_id)">删除</el-button>
                </td>
              </tr>
              <tr v-if="!loading && memories.length === 0"><td colspan="5" class="memory-empty">没有匹配的长期记忆。</td></tr>
            </tbody>
          </table>
        </div>
        <div v-if="canLoadMore && !error" class="memory-pagination">
          <el-button :loading="loadingMore" @click="loadMemories({ append: true })">加载更多</el-button>
        </div>
      </article>
    </section>

    <el-dialog v-model="editorVisible" :title="editing ? '查看 / 编辑偏好' : '新增偏好'" width="min(560px, calc(100vw - 32px))">
      <div v-loading="detailLoading">
        <el-alert v-if="editorError" :title="editorError" type="error" :closable="false" show-icon />
        <el-form v-else label-position="top">
          <el-form-item label="偏好内容">
            <el-input v-model="memoryForm.content" type="textarea" :rows="4" maxlength="4000" show-word-limit placeholder="例如：周一上午不安排会议" />
          </el-form-item>
          <el-form-item v-if="!editing" label="记录来源">
            <el-radio-group v-model="memoryForm.sourceType">
              <el-radio value="user_input">手动输入</el-radio>
              <el-radio value="user_confirmed">用户确认</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item v-if="!editing && memoryForm.sourceType === 'user_confirmed'" label="来源任务 ID">
            <el-input v-model="memoryForm.sourceRunId" placeholder="填写你拥有的 run_id" />
          </el-form-item>
          <el-form-item>
            <el-checkbox v-model="memoryForm.isSensitive">此偏好包含敏感信息</el-checkbox>
            <p class="memory-form-help">高危秘密（如密码、API Key、私钥、完整银行卡信息）不会被允许保存。</p>
          </el-form-item>
        </el-form>
      </div>
      <template #footer>
        <el-button @click="editorVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="detailLoading || !memoryForm.content.trim() || (memoryForm.sourceType === 'user_confirmed' && !memoryForm.sourceRunId.trim())"
          @click="saveMemory"
        >保存偏好</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="deleteVisible" title="确认删除长期记忆" width="min(480px, calc(100vw - 32px))">
      <el-alert v-if="deleteError" :title="deleteError" type="error" :closable="false" show-icon />
      <template v-else>
        <p>已创建一次性删除确认。确认后该记忆会被软删除，列表和后续查询将不再返回。</p>
        <p class="muted">确认令牌将在 {{ formatTime(deletionExpiresAt) }} 过期，且不会展示或保存到浏览器持久化存储。</p>
      </template>
      <template #footer>
        <el-button @click="deleteVisible = false">取消</el-button>
        <el-button type="danger" :loading="deleteLoading" @click="confirmDelete">确认删除</el-button>
      </template>
    </el-dialog>
  </main>
</template>
