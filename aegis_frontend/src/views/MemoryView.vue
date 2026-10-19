<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { memoryPayload, memoryConfirmPayload, memoryState, memoryFormFromRecord, memoryErrorMessage, memoryTypeLabel, memorySourceLabel, memoryStatusDisplay } from '../utils/memory'
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
const reviewed = ref(false)
const dirty = ref(false)
const conflict = ref(null)
const conflictRecord = ref(null)
const replaceReviewed = ref(false)
watch(memoryForm, () => { dirty.value = true; reviewed.value = false }, { deep: true, flush: 'sync' })
watch(editorVisible, (visible) => {
  if (!visible) {
    memoryForm.value = emptyMemoryForm()
    reviewed.value = false
    dirty.value = false
    conflict.value = null
    conflictRecord.value = null
  }
})

const canLoadMore = computed(() => Boolean(nextCursor.value))

function emptyMemoryForm() {
  return { content: '', isSensitive: false, memoryType: 'long_term_summary', entityKey: 'timezone',
    entityValue: 'Asia/Shanghai', contactName: '', contactEmail: '', expiresAt: null,
    sourceKind: 'manual', sourceObjectId: '', sourceVersion: '' }
}

function formatTime(value) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(new Date(value))
}

function detailMessage(cause, fallback) {
  return memoryErrorMessage(cause, fallback)
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
  dirty.value = false
  reviewed.value = false
  conflict.value = null
  conflictRecord.value = null
  editorVisible.value = true
}

async function openEditMemory(memoryId) {
  editorVisible.value = true
  detailLoading.value = true
  editorError.value = ''
  editing.value = null
  memoryForm.value = emptyMemoryForm()
  conflict.value = null
  conflictRecord.value = null
  try {
    const memory = await getMemory(memoryId)
    editing.value = memory
    memoryForm.value = memoryFormFromRecord(memory)
    dirty.value = false
    reviewed.value = false
    conflict.value = null
    conflictRecord.value = null
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
      editing.value = await updateMemory(editing.value.memory_id, memoryPayload(memoryForm.value, editing.value.version))
    } else {
      editing.value = await createMemory(memoryPayload(memoryForm.value))
    }
    // 用服务端实际保存/规范化后的值回填，核对的不是未保存输入或伪造来源版本。
    memoryForm.value = memoryFormFromRecord(editing.value)
    dirty.value = false
    reviewed.value = false
    conflict.value = null
    conflictRecord.value = null
    await loadMemories()
  } catch (cause) {
    editorError.value = detailMessage(cause, '保存长期记忆失败。')
  } finally {
    saving.value = false
  }
}

async function confirmSavedMemory() {
  if (!editing.value || dirty.value || !reviewed.value || editing.value.status !== 'pending') return
  if (conflict.value && (!conflictRecord.value || !replaceReviewed.value)) return
  saving.value = true
  editorError.value = ''
  try {
    editing.value = await confirmMemory(memoryConfirmPayload(editing.value, conflict.value ? conflictRecord.value : null))
    editorVisible.value = false
    await loadMemories()
  } catch (cause) {
    const detail = cause.response?.data?.detail
    editorError.value = detailMessage(cause, '确认失败，请重新核对。')
    if (detail?.code === 'MEMORY_ENTITY_CONFLICT') {
      conflict.value = detail
      conflictRecord.value = null
      replaceReviewed.value = false
    } else if (detail?.code === 'MEMORY_REPLACEMENT_STALE') {
      conflict.value = null
      conflictRecord.value = null
      replaceReviewed.value = false
      reviewed.value = false
    }
  } finally { saving.value = false }
}

async function reviewConflict() {
  try {
    conflictRecord.value = await getMemory(conflict.value.conflicting_memory_id)
    replaceReviewed.value = false
  } catch (cause) { editorError.value = detailMessage(cause, '旧记录已变，请刷新重试。') }
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
      <router-link class="nav" :to="{ name: 'today' }">▦ 今日工作台</router-link>
      <router-link class="nav" :to="{ name: 'tasks' }">◌ 任务对话</router-link>
      <router-link class="nav" :to="{ name: 'calendar' }">▦ 日历</router-link>
      <router-link class="nav" :to="{ name: 'mail-management' }">✉ 邮件管理</router-link>
      <router-link class="nav" :to="{ name: 'todo-plans' }">☷ 待办计划</router-link>
      <router-link class="nav" :to="{ name: 'confirmations' }">✓ 操作确认</router-link>
      <router-link class="nav" :to="{ name: 'knowledge' }">▤ 知识资料</router-link>
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
          <p class="muted">仅由你主动维护。保存或编辑后待确认，核对确认后生效；敏感、过期或来源失效的记录不进入模型。设置项保存为偏好，不修改连接权限或服务器时区。</p>
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
            <thead><tr><th>记忆内容</th><th>类型 / 状态</th><th>更新时间 / 有效期</th><th>敏感性</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="memory in memories" :key="memory.memory_id">
                <td>
                  <div class="title">{{ memory.content_preview }}</div>
                  <div v-if="memory.is_sensitive" class="detail">敏感正文不会随列表返回，点击“查看 / 编辑”后按需读取。</div>
                </td>
                <td>
                  <div class="memory-classification">
                    <div class="memory-classification-tags">
                      <span class="memory-type-tag">{{ memoryTypeLabel(memory) }}</span>
                      <span class="memory-status-tag" :class="memoryStatusDisplay(memory).tone">{{ memoryStatusDisplay(memory).label }}</span>
                    </div>
                    <span class="memory-source">来源：{{ memorySourceLabel(memory) }}</span>
                    <span v-if="memoryStatusDisplay(memory).note" class="memory-status-note">{{ memoryStatusDisplay(memory).note }}</span>
                  </div>
                </td>
                <td>
                  <div class="memory-time-info">
                    <time v-if="memory.updated_at" :datetime="memory.updated_at" class="memory-updated-at">{{ formatTime(memory.updated_at) }}</time>
                    <span v-else class="memory-updated-at">更新时间未知</span>
                    <span class="memory-expiry" :class="{ 'has-expiry': memory.expires_at }">
                      {{ memory.expires_at ? `有效期至 ${formatTime(memory.expires_at)}` : '长期有效' }}
                    </span>
                  </div>
                </td>
                <td><span class="memory-sensitivity" :class="memory.is_sensitive ? 'sensitive' : 'normal'">{{ memory.is_sensitive ? '敏感' : '普通' }}</span></td>
                <td class="memory-actions">
                  <div class="memory-action-group">
                    <el-button size="small" plain type="primary" @click="openEditMemory(memory.memory_id)">查看 / 编辑</el-button>
                    <el-button size="small" text type="danger" :loading="deleteLoading" @click="requestDelete(memory.memory_id)">删除</el-button>
                  </div>
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

    <el-dialog v-model="editorVisible" :close-on-click-modal="!saving && !detailLoading" :close-on-press-escape="!saving && !detailLoading" :show-close="!saving && !detailLoading" :title="editing ? '查看 / 编辑偏好' : '新增偏好'" width="min(560px, calc(100vw - 32px))">
      <div v-loading="detailLoading">
        <el-alert v-if="editorError" :title="editorError" type="error" :closable="false" show-icon />
        <el-form v-if="!detailLoading" :disabled="saving" label-position="top">
          <p v-if="editing">版本 {{ editing.version }} · {{ memoryState(editing) }}</p>
          <el-form-item label="记忆类型">
            <el-select v-model="memoryForm.memoryType">
              <el-option label="长期摘要 / 偏好" value="long_term_summary" />
              <el-option label="结构化用户设置" value="entity" />
            </el-select>
          </el-form-item>
          <template v-if="memoryForm.memoryType === 'entity'">
            <el-form-item label="设置项（用户范围内每项仅一个生效值）">
              <el-select v-model="memoryForm.entityKey" @change="memoryForm.entityValue = memoryForm.entityKey === 'reply_style' ? 'formal' : 'Asia/Shanghai'">
                <el-option label="默认时区" value="timezone" /><el-option label="邮件语气" value="reply_style" /><el-option label="常用联系人" value="contact" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="memoryForm.entityKey === 'timezone'" label="IANA时区"><el-input v-model="memoryForm.entityValue" placeholder="Asia/Shanghai" /></el-form-item>
            <el-form-item v-if="memoryForm.entityKey === 'reply_style'" label="语气"><el-select v-model="memoryForm.entityValue"><el-option label="正式" value="formal" /><el-option label="简洁" value="concise" /><el-option label="友好" value="friendly" /></el-select></el-form-item>
            <template v-if="memoryForm.entityKey === 'contact'"><el-form-item label="联系人姓名"><el-input v-model="memoryForm.contactName" /></el-form-item><el-form-item label="邮箱"><el-input v-model="memoryForm.contactEmail" /></el-form-item></template>
          </template>
          <el-form-item label="偏好内容">
            <el-input v-model="memoryForm.content" type="textarea" :rows="4" maxlength="4000" show-word-limit placeholder="例如：周一上午不安排会议" />
          </el-form-item>
          <el-form-item label="有效期（留空表示长期有效）"><el-date-picker v-model="memoryForm.expiresAt" type="datetime" clearable /></el-form-item>
          <el-form-item label="录入来源（不会标记为模型提取）">
            <el-select v-model="memoryForm.sourceKind"><el-option label="纯手动录入" value="manual" /><el-option label="显式引用本人已完成任务" value="run" /><el-option label="显式引用已授权知识资料" value="knowledge_document" /></el-select>
          </el-form-item>
          <template v-if="memoryForm.sourceKind !== 'manual'"><el-form-item label="真实来源对象 ID"><el-input v-model="memoryForm.sourceObjectId" /></el-form-item><el-form-item label="来源版本（资料填写版本UUID；任务留空由服务端核验当前版本）"><el-input v-model="memoryForm.sourceVersion" /></el-form-item></template>
          <el-form-item>
            <el-checkbox v-model="memoryForm.isSensitive">此偏好包含敏感信息</el-checkbox>
            <p class="memory-form-help">高危秘密（如密码、API Key、私钥、完整银行卡信息）不会被允许保存。</p>
          </el-form-item>
        </el-form>
        <el-checkbox v-if="editing?.status === 'pending'" v-model="reviewed" :disabled="dirty">我已核对已保存的内容、字段、来源和有效期，确认生效</el-checkbox>
        <div v-if="conflict">
          <p>同一设置已有确认值，请先查看旧记录，再明确选择替换。</p>
          <el-button @click="reviewConflict">查看待替换记录</el-button>
          <template v-if="conflictRecord"><p>{{ conflictRecord.content }}</p><pre>{{ JSON.stringify(conflictRecord.entity, null, 2) }}</pre><p>版本 {{ conflictRecord.version }} · {{ memoryState(conflictRecord) }}</p><el-checkbox v-model="replaceReviewed">我明确替换这条旧记录，不合并两者</el-checkbox></template>
        </div>
      </div>
      <template #footer>
        <el-button :disabled="saving || detailLoading" @click="editorVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="detailLoading || !memoryForm.content.trim() || (editing && !['pending','confirmed','revoked'].includes(editing.status))"
          @click="saveMemory"
        >保存（待确认）</el-button>
        <el-button type="primary" :loading="saving" :disabled="!editing || editing.status !== 'pending' || dirty || !reviewed || (conflict && (!conflictRecord || !replaceReviewed))" @click="confirmSavedMemory">确认生效</el-button>
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

<style scoped>
.memory-classification { display: grid; justify-items: start; gap: 6px; min-width: 150px; }
.memory-classification-tags { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.memory-type-tag, .memory-status-tag { display: inline-flex; align-items: center; min-height: 23px; padding: 2px 8px; border-radius: 5px; font-size: 11px; font-weight: 700; line-height: 1.35; white-space: nowrap; }
.memory-type-tag { background: #eef4fc; color: #275b96; }
.memory-status-tag.confirmed { background: #e8f7ef; color: #17795d; }
.memory-status-tag.pending { background: #fff4e4; color: #9a5b10; }
.memory-status-tag.unavailable { background: #fff0f0; color: #b42318; }
.memory-status-tag.inactive { background: #f1f4f8; color: #526176; }
.memory-source { color: #667085; font-size: 11px; }
.memory-status-note { color: #a22d68; font-size: 11px; }
.memory-table th:nth-child(3), .memory-table td:nth-child(3) { min-width: 190px; }
.memory-table th:nth-child(4), .memory-table td:nth-child(4) { min-width: 90px; }
.memory-table th:nth-child(5), .memory-table td:nth-child(5) { min-width: 170px; }
.memory-time-info { display: grid; justify-items: start; gap: 5px; white-space: nowrap; }
.memory-updated-at { color: #243952; font-size: 13px; font-weight: 650; }
.memory-expiry { color: #667085; font-size: 11px; }
.memory-expiry.has-expiry { color: #526176; }
.memory-sensitivity { display: inline-flex; align-items: center; gap: 6px; padding: 4px 9px; border-radius: 99px; font-size: 11px; font-weight: 700; white-space: nowrap; }
.memory-sensitivity::before { width: 6px; height: 6px; border-radius: 50%; background: currentColor; content: ''; }
.memory-sensitivity.normal { background: #f1f4f8; color: #526176; }
.memory-sensitivity.sensitive { background: #fff0f7; color: #a22d68; }
.memory-action-group { display: flex; align-items: center; gap: 8px; }
.memory-action-group :deep(.el-button + .el-button) { margin-left: 0; }
</style>
