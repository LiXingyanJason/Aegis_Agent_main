<script setup>
import { computed, h, onMounted, ref, watch } from 'vue'
import { memoryPayload, memoryConfirmPayload, memoryFormFromRecord, memoryErrorMessage, memoryTypeLabel, memorySourceLabel, memoryStatusDisplay, memoryTimezoneOptions } from '../utils/memory'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import {
  confirmMemory,
  createMemory,
  deleteMemory,
  getMemory,
  listMemories,
  requestMemoryDeletion,
  updateMemory,
  revokeMemory,
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
const revokingId = ref(null)
const editorError = ref('')
const editing = ref(null)
const deleteVisible = ref(false)
const deleteLoading = ref(false)
const deleteError = ref('')
const deletionToken = ref('')
const deletionExpiresAt = ref(null)
const memoryForm = ref(emptyMemoryForm())
const confirmingId = ref(null)
const dirty = ref(false)
const conflict = ref(null)
const conflictTarget = ref(null)
const conflictRecord = ref(null)
const conflictVisible = ref(false)
const conflictError = ref('')
const replaceReviewed = ref(false)
watch(memoryForm, () => { dirty.value = true }, { deep: true, flush: 'sync' })
watch(editorVisible, (visible) => {
  if (!visible) {
    memoryForm.value = emptyMemoryForm()
    dirty.value = false
  }
})
watch(conflictVisible, (visible) => {
  if (!visible) {
    conflict.value = null
    conflictTarget.value = null
    conflictRecord.value = null
    conflictError.value = ''
    replaceReviewed.value = false
  }
})

const canLoadMore = computed(() => Boolean(nextCursor.value))
const timezoneOptions = computed(() => memoryTimezoneOptions(memoryForm.value.entityKey === 'timezone' ? memoryForm.value.entityValue : null))

function emptyMemoryForm() {
  return { content: '', isSensitive: false, memoryType: 'long_term_summary', entityKey: 'timezone',
    entityValue: 'Asia/Shanghai', contactName: '', contactEmail: '', expiresAt: null,
    meetingTimeOfDay: 'any', meetingAvoidWeekdays: [],
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
  editorVisible.value = true
}

async function openEditMemory(memoryId) {
  editorVisible.value = true
  detailLoading.value = true
  editorError.value = ''
  editing.value = null
  memoryForm.value = emptyMemoryForm()
  try {
    const memory = await getMemory(memoryId)
    editing.value = memory
    memoryForm.value = memoryFormFromRecord(memory)
    dirty.value = false
  } catch (cause) {
    editorError.value = detailMessage(cause, '加载记忆详情失败。')
  } finally {
    detailLoading.value = false
  }
}

async function saveMemory() {
  const content = memoryForm.value.content.trim()
  if (memoryForm.value.memoryType !== 'entity' && !content) return
  const wasRevoked = editing.value?.status === 'revoked'
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
    await loadMemories()
    if (wasRevoked) editorVisible.value = false
  } catch (cause) {
    editorError.value = detailMessage(cause, '保存长期记忆失败。')
  } finally {
    saving.value = false
  }
}

async function cancelConfirmation(memory) {
  if (revokingId.value) return
  try {
    await ElMessageBox.confirm('取消后，此记忆不再用于后续模型调用。以后可再次核对并确认生效。', '取消记忆生效', { confirmButtonText: '取消生效', cancelButtonText: '保持生效', type: 'warning' })
  } catch { return }
  revokingId.value = memory.memory_id
  try {
    await revokeMemory(memory.memory_id, memory.version)
    await loadMemories()
  } catch (cause) {
    if (cause.response?.status === 409) {
      await loadMemories()
      error.value = error.value
        ? '记忆状态或版本已变化，刷新失败；请重试刷新后核对当前记录。'
        : '记忆状态或版本已变化，列表已刷新；请核对当前记录后再操作。'
    } else {
      error.value = memoryErrorMessage(cause, '取消确认失败，请刷新后核对版本。')
    }
  } finally { revokingId.value = null }
}

function savedMemoryReview(memory) {
  return h('div', { style: { maxHeight: '55vh', overflowY: 'auto', overflowWrap: 'anywhere', textAlign: 'left' } }, [
    h('p', '请核对服务端已保存的内容、字段、来源和有效期。确认后，此记忆将用于后续符合条件的任务。'),
    h('p', `内容：${memory.content || '—'}`),
    h('p', `类型：${memoryTypeLabel(memory)} · 敏感性：${memory.is_sensitive ? '敏感（不进入模型）' : '普通'}`),
    memory.entity ? h('p', `设置：${JSON.stringify(memory.entity)}`) : null,
    h('p', `来源：${memorySourceLabel(memory)} · 有效期：${memory.expires_at ? formatTime(memory.expires_at) : '长期有效'}`),
    h('p', `版本：${memory.version}`),
  ])
}

async function confirmListedMemory(memory) {
  if (!['pending', 'revoked'].includes(memory.status) || confirmingId.value) return
  confirmingId.value = memory.memory_id
  try {
    const saved = await getMemory(memory.memory_id)
    if (saved.status !== memory.status || saved.version !== memory.version) {
      await loadMemories()
      error.value = '记忆状态或版本已变化，请核对刷新后的记录再操作。'
      return
    }
    try {
      await ElMessageBox.confirm(savedMemoryReview(saved), saved.status === 'revoked' ? '重新确认记忆生效' : '确认记忆生效', {
        confirmButtonText: '确认生效', cancelButtonText: '再检查一下', type: 'warning',
      })
    } catch { return }
    let target = saved
    if (saved.status === 'revoked') {
      // 服务端不接受直接确认 revoked；先用已核对的详情保存为 pending，再用返回的新版本确认。
      target = await updateMemory(saved.memory_id, memoryPayload(memoryFormFromRecord(saved), saved.version))
      await loadMemories()
    }
    await submitMemoryConfirmation(target)
  } catch (cause) {
    error.value = detailMessage(cause, '确认失败，请刷新列表后重试。')
  } finally { confirmingId.value = null }
}

async function submitMemoryConfirmation(target, replacement = null) {
  try {
    await confirmMemory(memoryConfirmPayload(target, replacement))
    conflictVisible.value = false
    conflict.value = null
    conflictTarget.value = null
    conflictRecord.value = null
    await loadMemories()
  } catch (cause) {
    const detail = cause.response?.data?.detail
    if (detail?.code === 'MEMORY_ENTITY_CONFLICT') {
      conflict.value = detail
      conflictTarget.value = target
      conflictRecord.value = null
      replaceReviewed.value = false
      conflictError.value = ''
      conflictVisible.value = true
    } else if (detail?.code === 'MEMORY_REPLACEMENT_STALE') {
      conflict.value = null
      conflictTarget.value = null
      conflictRecord.value = null
      replaceReviewed.value = false
      conflictVisible.value = false
      await loadMemories()
      error.value = '待替换的记录已变化，请重新核对后再确认。'
    } else {
      const message = detailMessage(cause, '确认失败，请重新核对。')
      if (cause.response?.status === 409) await loadMemories()
      if (conflictVisible.value) conflictError.value = message
      else error.value = message
    }
  }
}

async function reviewConflict() {
  try {
    const record = await getMemory(conflict.value.conflicting_memory_id)
    if (record.version !== conflict.value.conflicting_version || record.status !== 'confirmed') {
      conflictRecord.value = null
      replaceReviewed.value = false
      conflictError.value = '待替换记录已变化，请关闭后重新确认。'
      return
    }
    conflictRecord.value = record
    replaceReviewed.value = false
  } catch (cause) { conflictError.value = detailMessage(cause, '旧记录已变，请刷新重试。') }
}

async function confirmReplacement() {
  if (!conflictTarget.value || !conflictRecord.value || !replaceReviewed.value || confirmingId.value) return
  const target = conflictTarget.value
  const replacement = conflictRecord.value
  confirmingId.value = target.memory_id
  try {
    try {
      await ElMessageBox.confirm('确认以这条新设置替换已核对的旧记录？两条记录不会合并。', '确认替换并生效', {
        confirmButtonText: '替换并生效', cancelButtonText: '再检查一下', type: 'warning',
      })
    } catch { return }
    await submitMemoryConfirmation(target, replacement)
  } finally { confirmingId.value = null }
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
          <p class="muted">仅由你主动维护。保存或编辑后待确认，核对确认后生效；取消确认后不再用于后续模型调用。敏感、过期或来源失效的记录不进入模型。相关的结构化设置优先于同主题摘要，用户当前明确要求始终优先于偏好；设置不修改连接权限或服务器时区。</p>
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
                    <el-button v-if="memory.status === 'pending' || memory.status === 'revoked'" size="small" text type="primary" :disabled="Boolean(confirmingId)" :loading="confirmingId === memory.memory_id" @click="confirmListedMemory(memory)">确认生效</el-button>
                    <el-button v-else-if="memory.status === 'confirmed'" size="small" text :disabled="Boolean(revokingId)" :loading="revokingId === memory.memory_id" @click="cancelConfirmation(memory)">取消生效</el-button>
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
        <el-alert v-if="editing?.status === 'revoked'" title="此记忆已撤销。核对后先保存为待确认，再到列表点击“确认生效”；保存本身不会立即生效。" type="info" :closable="false" show-icon />
        <el-form v-if="!detailLoading" :disabled="saving" label-position="top">
          <p v-if="editing">版本 {{ editing.version }} · {{ memoryStatusDisplay(editing).label }}<span v-if="memoryStatusDisplay(editing).note">（{{ memoryStatusDisplay(editing).note }}）</span></p>
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
                <el-option label="会议软偏好" value="meeting_preferences" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="memoryForm.entityKey === 'timezone'" label="时区">
              <el-select v-model="memoryForm.entityValue" filterable placeholder="请选择时区，可搜索城市或时区名称" style="width: 100%">
                <el-option v-for="option in timezoneOptions" :key="option.value" :label="option.label" :value="option.value" />
              </el-select>
            </el-form-item>
            <p v-if="memoryForm.entityKey === 'timezone'">确认后用于今后日历任务中未注明时区的输入；本轮明确时区优先，不修改已有或已批准的会议。</p>
            <el-form-item v-if="memoryForm.entityKey === 'reply_style'" label="语气"><el-select v-model="memoryForm.entityValue"><el-option label="正式" value="formal" /><el-option label="简洁" value="concise" /><el-option label="友好" value="friendly" /></el-select></el-form-item>
            <template v-if="memoryForm.entityKey === 'contact'"><el-form-item label="联系人姓名"><el-input v-model="memoryForm.contactName" /></el-form-item><el-form-item label="邮箱"><el-input v-model="memoryForm.contactEmail" /></el-form-item></template>
            <template v-if="memoryForm.entityKey === 'meeting_preferences'">
              <el-form-item label="偏好时段"><el-select v-model="memoryForm.meetingTimeOfDay">
                <el-option label="无偏好" value="any" /><el-option label="上午" value="morning" /><el-option label="下午" value="afternoon" /><el-option label="晚上" value="evening" />
              </el-select></el-form-item>
              <el-form-item label="尽量避开的星期"><el-select v-model="memoryForm.meetingAvoidWeekdays" multiple clearable>
                <el-option v-for="(label,index) in ['周一','周二','周三','周四','周五','周六','周日']" :key="index" :label="label" :value="index+1" />
              </el-select></el-form-item>
              <p>仅作为候选推荐的软偏好；不代表空闲、不改变明确要求，也不批准创建会议。</p>
            </template>
          </template>
          <el-form-item :label="memoryForm.memoryType === 'entity' ? '补充说明（可选）' : '偏好内容（必填）'">
            <el-input v-model="memoryForm.content" type="textarea" :rows="4" maxlength="4000" show-word-limit :placeholder="memoryForm.memoryType === 'entity' ? '留空时由系统根据设置自动生成说明' : '例如：周一上午不安排会议'" />
          </el-form-item>
          <el-form-item v-if="memoryForm.memoryType === 'entity' && editing?.content_is_generated && !memoryForm.content.trim() && !dirty" label="已保存的系统说明">
            <span>{{ editing.content }}</span>
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
      </div>
      <template #footer>
        <el-button :disabled="saving || detailLoading" @click="editorVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="detailLoading || (memoryForm.memoryType !== 'entity' && !memoryForm.content.trim()) || (editing && !['pending','confirmed','revoked'].includes(editing.status))"
          @click="saveMemory"
        >{{ editing?.status === 'revoked' ? '保存为待确认' : '保存（待确认）' }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="conflictVisible" title="核对待替换的设置" width="min(560px, calc(100vw - 32px))">
      <el-alert v-if="conflictError" :title="conflictError" type="error" :closable="false" show-icon />
      <p>同一设置已有确认值。请查看旧记录，并明确选择是否替换。</p>
      <p v-if="conflictTarget">新记录：{{ conflictTarget.content }} · 版本 {{ conflictTarget.version }}</p>
      <el-button :disabled="Boolean(confirmingId)" @click="reviewConflict">查看待替换记录</el-button>
      <template v-if="conflictRecord">
        <p>旧记录：{{ conflictRecord.content }}</p>
        <pre v-if="conflictRecord.entity">{{ JSON.stringify(conflictRecord.entity, null, 2) }}</pre>
        <p>版本 {{ conflictRecord.version }} · {{ memoryStatusDisplay(conflictRecord).label }}<span v-if="memoryStatusDisplay(conflictRecord).note">（{{ memoryStatusDisplay(conflictRecord).note }}）</span></p>
        <el-checkbox v-model="replaceReviewed">我明确替换这条旧记录，不合并两者</el-checkbox>
      </template>
      <template #footer>
        <el-button :disabled="Boolean(confirmingId)" @click="conflictVisible = false">取消</el-button>
        <el-button type="primary" :loading="Boolean(confirmingId)" :disabled="!conflictRecord || !replaceReviewed" @click="confirmReplacement">替换并生效</el-button>
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
