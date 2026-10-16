<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getKnowledgeIndexJob,
  importKnowledgeText,
  listKnowledgeDocuments,
  retryKnowledgeIndex,
  setKnowledgeActivation,
  startKnowledgeIndex,
  uploadKnowledgeFile,
  getKnowledgeDocument, deleteKnowledgeDocument, setKnowledgeVisibility,
} from '../api/knowledge'
import { useAuthStore } from '../store/auth'

const auth = useAuthStore()
const router = useRouter()
const mode = ref('file')
const fileInput = ref(null)
const selectedFile = ref(null)
const title = ref('')
const visibility = ref('private')
const content = ref('')
const isSensitive = ref(false)
const requestId = ref(crypto.randomUUID())
const search = ref('')
const statusFilter = ref('all')
const documents = ref([])
const offset = ref(0)
const documentDetail = ref(null)
const detailOpen = ref(false)
const detailError = ref('')
let detailRequest = 0
const jobs = ref({})
const loading = ref(false)
const uploading = ref(false)
const busyDocumentId = ref(null)
const listError = ref('')
const importError = ref('')
const actionErrors = ref({})
const jobErrors = ref({})
let pollTimer = null
let pollInFlight = false
let mounted = false

const filteredDocuments = computed(() => documents.value.filter((document) => {
  const matchesText = document.title.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase())
  const matchesStatus = statusFilter.value === 'all' || document.index_status === statusFilter.value
  return matchesText && matchesStatus
}))

const canImport = computed(() => Boolean(
  title.value.trim()
  && (mode.value === 'file' ? selectedFile.value : content.value.trim())
  && !uploading.value,
))

function statusLabel(value) {
  return {
    not_indexed: '待索引', queued: '排队中', running: '向量化中',
    ready: '已向量化', failed: '索引失败', cancelled: '已取消', unavailable: '资料不可用',
  }[value] ?? value
}

function formatTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: 'numeric', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  }).format(date)
}

function errorMessage(cause, fallback) {
  const detail = cause?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail.message === 'string') return detail.message
  if (cause?.response?.status === 404) return '资料或任务不存在，或你没有操作权限。'
  return cause?.message || fallback
}

function setActionError(documentId, message = '') {
  actionErrors.value = { ...actionErrors.value, [documentId]: message }
}

function applyJobStatus(job) {
  // 成功任务仍需由资料列表确认索引可用，不能仅凭job推断ready。
  if (!['failed', 'cancelled'].includes(job.status)) return
  const status = job.status
  documents.value = documents.value.map((item) => item.document_id === job.document_id
    ? { ...item, index_status: status } : item)
}

async function loadDocuments({ silent = false } = {}) {
  if (!silent) loading.value = true
  listError.value = ''
  try {
    const items = await listKnowledgeDocuments(100, offset.value)
    if (!mounted) return
    documents.value = Array.isArray(items) ? items : []
    const activeIds = new Set(documents.value.map((item) => item.job_id).filter(Boolean))
    jobs.value = Object.fromEntries(Object.entries(jobs.value).filter(([id]) => activeIds.has(id)))
    // 失败任务的错误码与进行中任务的片段数只由独立job接口提供。
    const inspect = documents.value.filter((item) => item.can_manage && item.job_id
      && ['queued', 'running', 'failed'].includes(item.index_status))
    await Promise.all(inspect.map(async (item) => {
      try {
        const job = await getKnowledgeIndexJob(item.job_id)
        if (mounted) {
          jobs.value = { ...jobs.value, [item.job_id]: job }
          jobErrors.value = { ...jobErrors.value, [item.job_id]: '' }
          if (['succeeded', 'failed', 'cancelled'].includes(job.status)) applyJobStatus(job)
        }
      } catch (cause) {
        if (mounted) jobErrors.value = { ...jobErrors.value, [item.job_id]: errorMessage(cause, '索引任务状态暂不可用。') }
      }
    }))
  } catch (cause) {
    if (mounted) listError.value = errorMessage(cause, '加载资料列表失败。')
  } finally {
    if (mounted) loading.value = false
  }
}

async function pollJobs() {
  if (!mounted || pollInFlight) return
  const active = documents.value.filter((item) => item.can_manage && item.job_id
    && ['queued', 'running'].includes(item.index_status)
    && !['succeeded', 'failed', 'cancelled'].includes(jobs.value[item.job_id]?.status))
  if (!active.length) return
  pollInFlight = true
  let hasTerminal = false
  try {
    await Promise.all(active.map(async (item) => {
      try {
        const job = await getKnowledgeIndexJob(item.job_id)
        if (!mounted) return
        jobs.value = { ...jobs.value, [item.job_id]: job }
        jobErrors.value = { ...jobErrors.value, [item.job_id]: '' }
        if (['succeeded', 'failed', 'cancelled'].includes(job.status)) {
          applyJobStatus(job)
          hasTerminal = true
        }
      } catch (cause) {
        if (mounted) jobErrors.value = { ...jobErrors.value, [item.job_id]: errorMessage(cause, '索引任务状态暂不可用。') }
      }
    }))
    if (hasTerminal && mounted) await loadDocuments({ silent: true })
  } finally {
    pollInFlight = false
  }
}

function chooseFile() {
  fileInput.value?.click()
}

function onFileChange(event) {
  const file = event.target.files?.[0]
  if (!file) return
  if (!/\.(md|txt)$/i.test(file.name)) {
    importError.value = '仅支持 .md 和 .txt 文件。'
  } else if (!file.size || file.size > 262_144) {
    importError.value = '请选择非空且不超过 256 KiB 的文件。'
  } else {
    importError.value = ''
    selectedFile.value = file
    if (!title.value.trim()) title.value = file.name.replace(/\.(md|txt)$/i, '').slice(0, 200)
    return
  }
  selectedFile.value = null
  event.target.value = ''
}

async function importDocument() {
  if (!canImport.value) return
  if (mode.value === 'text' && new TextEncoder().encode(content.value).length > 262_144) {
    importError.value = '文本内容不得超过 256 KiB。'
    return
  }
  uploading.value = true
  importError.value = ''
  const metadata = {
    title: title.value.trim(), visibility: visibility.value,
    client_request_id: requestId.value, is_sensitive: isSensitive.value,
    external_embedding_allowed: false,
  }
  try {
    const result = mode.value === 'file'
      ? await uploadKnowledgeFile(selectedFile.value, metadata)
      : await importKnowledgeText({ ...metadata, content: content.value })
    title.value = ''
    content.value = ''
    selectedFile.value = null
    if (fileInput.value) fileInput.value.value = ''
    isSensitive.value = false
    requestId.value = crypto.randomUUID()
    ElMessage.success(result.idempotent_replay ? '资料已存在，已同步当前状态。' : '资料已保存；建立索引和启用需分别操作。')
    await loadDocuments({ silent: true })
  } catch (cause) {
    importError.value = errorMessage(cause, '保存资料失败。')
    if (cause?.response?.data?.detail?.code === 'KNOWLEDGE_IDEMPOTENCY_CONFLICT') requestId.value = crypto.randomUUID()
  } finally {
    uploading.value = false
  }
}

async function confirmIndex(document) {
  try {
    await ElMessageBox.confirm(
      `建立「${document.title}」索引会将资料内容提供给外部向量化服务。确认授权并开始吗？`,
      '确认外发并建立索引',
      { confirmButtonText: '同意并建立索引', cancelButtonText: '取消', type: 'warning' },
    )
  } catch { return }
  busyDocumentId.value = document.document_id
  setActionError(document.document_id)
  try {
    const result = await startKnowledgeIndex(document.document_id)
    ElMessage.success(result.idempotent_replay ? '已复用现有索引任务。' : '索引任务已提交；完成后仍需手动启用资料。')
    await loadDocuments({ silent: true })
  } catch (cause) {
    setActionError(document.document_id, errorMessage(cause, '建立索引失败。'))
  } finally {
    busyDocumentId.value = null
  }
}

async function retryIndex(document) {
  if (!document.job_id) return
  busyDocumentId.value = document.document_id
  setActionError(document.document_id)
  try {
    await retryKnowledgeIndex(document.job_id)
    ElMessage.success('索引任务已重新排队，资料仍未启用。')
    await loadDocuments({ silent: true })
  } catch (cause) {
    setActionError(document.document_id, errorMessage(cause, '重试索引失败。'))
  } finally {
    busyDocumentId.value = null
  }
}

async function toggleEnabled(document) {
  busyDocumentId.value = document.document_id
  setActionError(document.document_id)
  try {
    const result = await setKnowledgeActivation(document.document_id, !document.enabled)
    documents.value = documents.value.map((item) => item.document_id === document.document_id
      ? { ...item, enabled: result.enabled, index_status: result.index_status } : item)
    ElMessage.success(result.enabled ? '资料已启用。' : '资料已禁用，新的检索将不再使用它。')
  } catch (cause) {
    setActionError(document.document_id, errorMessage(cause, '修改资料启用状态失败。'))
  } finally {
    busyDocumentId.value = null
  }
}

async function signOut() {
  await auth.signOut()
  await router.replace({ name: 'login' })
}

async function showDetail(document) {
  const request = ++detailRequest
  documentDetail.value = null
  detailError.value = ''
  detailOpen.value = true
  try {
    const result = await getKnowledgeDocument(document.document_id)
    if (request === detailRequest && detailOpen.value) documentDetail.value = result
  } catch (cause) {
    if (request === detailRequest) detailError.value = errorMessage(cause, '资料详情暂不可用。')
  }
}

async function shareDocument(document) {
  const next = document.visibility === 'tenant' ? 'private' : 'tenant'
  try {
    await ElMessageBox.confirm(next === 'tenant' ? '允许同租户成员读取此资料吗？启用状态仍独立控制。' : '收回共享后，其他成员的新检索与来源读取将被拒绝。', '更改可见范围')
  } catch { return }
  busyDocumentId.value = document.document_id
  try {
    await setKnowledgeVisibility(document.document_id, next)
    await loadDocuments({ silent: true })
  } catch (cause) { setActionError(document.document_id, errorMessage(cause, '更改范围失败。')) }
  finally { busyDocumentId.value = null }
}

async function removeDocument(document) {
  try {
    await ElMessageBox.confirm(`删除「${document.title}」后立即停止新的检索和来源读取。源文件与审计保留，不物理清除向量。`, '确认删除资料', { type: 'warning' })
  } catch { return }
  busyDocumentId.value = document.document_id
  try {
    await deleteKnowledgeDocument(document.document_id)
    await loadDocuments({ silent: true })
    ElMessage.success('资料已移除，后续访问已停止。')
  } catch (cause) { setActionError(document.document_id, errorMessage(cause, '删除资料失败。')) }
  finally { busyDocumentId.value = null }
}

async function changePage(delta) {
  offset.value = Math.max(0, offset.value + delta * 100)
  await loadDocuments()
}

onMounted(() => {
  mounted = true
  loadDocuments()
  pollTimer = window.setInterval(pollJobs, 2_000)
})

onUnmounted(() => {
  mounted = false
  if (pollTimer) window.clearInterval(pollTimer)
})
</script>

<template>
  <main class="workspace">
    <el-dialog v-model="detailOpen" title="资料详情" @close="detailRequest += 1; documentDetail = null">
      <p v-if="detailError" role="alert">{{ detailError }}</p>
      <template v-else-if="documentDetail">
        <h3>{{ documentDetail.title }}</h3>
        <p>{{ documentDetail.version_label }} · {{ statusLabel(documentDetail.index_status) }}</p>
        <p>已索引 {{ documentDetail.indexed_chunks }} 个片段 · {{ documentDetail.enabled ? '已启用' : '未启用' }}</p>
        <p>{{ documentDetail.visibility === 'tenant' ? '同租户共享' : '私有' }}；管理权以服务端核验为准。</p>
      </template>
      <p v-else>正在读取…</p>
    </el-dialog>
    <aside class="sidebar">
      <div class="brand"><span class="mark">⌾</span>Aegis PA</div>
      <p class="nav-label app-nav-label">工作空间</p>
      <router-link class="nav" :to="{ name: 'today' }">▦ 今日工作台</router-link>
      <router-link class="nav" :to="{ name: 'tasks' }">◌ 任务对话</router-link>
      <router-link class="nav" :to="{ name: 'calendar' }">▦ 日历</router-link>
      <router-link class="nav" :to="{ name: 'mail-management' }">✉ 邮件管理</router-link>
      <router-link class="nav" :to="{ name: 'todo-plans' }">☷ 待办计划</router-link>
      <router-link class="nav" :to="{ name: 'confirmations' }">✓ 操作确认</router-link>
      <router-link class="nav active" :to="{ name: 'knowledge' }">▤ 知识资料</router-link>
      <router-link class="nav" :to="{ name: 'memories' }">◇ 长期记忆</router-link>
      <span class="nav disabled">◫ 连接与审计（待接入）</span>

      <div class="identity">
        <b>{{ auth.profile?.name }}</b>
        <span>{{ auth.profile?.email || '已通过 Keycloak 登录' }}</span>
        <el-button text type="primary" @click="signOut">退出登录</el-button>
      </div>
    </aside>

    <section class="content knowledge-content">
      <header class="page-header">
        <div>
          <h1>知识资料</h1>
          <p class="muted">上传资料、查看索引状态，并管理后续可用的知识来源。</p>
        </div>
        <span class="badge read">资料管理</span>
      </header>

      <el-alert class="knowledge-notice" title="上传、建立索引、启用是三个独立步骤" description="上传仅保存资料，不会自动向外部发送内容。建立索引需再次确认外发；索引完成后仍需手动启用。知识检索能力是否开放由服务端配置决定。" type="info" :closable="false" show-icon />

      <div class="knowledge-layout">
        <section class="card knowledge-import" aria-label="导入资料">
          <div class="knowledge-card-head">
            <div>
              <h2>上传资料</h2>
              <p>选择本地文件，或直接粘贴文本。</p>
            </div>
          </div>
          <div class="knowledge-import-body">
            <div class="knowledge-mode-switch" role="tablist" aria-label="资料导入方式">
              <button type="button" role="tab" :aria-selected="mode === 'file'" :class="{ active: mode === 'file' }" @click="mode = 'file'">本地文件</button>
              <button type="button" role="tab" :aria-selected="mode === 'text'" :class="{ active: mode === 'text' }" @click="mode = 'text'">文本输入</button>
            </div>

            <div v-if="mode === 'file'" class="knowledge-dropzone">
              <span class="knowledge-file-icon" aria-hidden="true">▤</span>
              <b>{{ selectedFile?.name || '选择一份资料文件' }}</b>
              <p>{{ selectedFile ? `${(selectedFile.size / 1024).toFixed(1)} KiB · 上传后仅保存源文件` : '支持非空 UTF-8 的 .md / .txt，最大 256 KiB' }}</p>
              <input ref="fileInput" type="file" accept=".md,.txt,text/plain,text/markdown" class="knowledge-file-input" @change="onFileChange">
              <el-button @click="chooseFile">选择文件</el-button>
            </div>
            <div v-else class="knowledge-text-entry">
              <label for="knowledge-content">资料内容</label>
              <el-input id="knowledge-content" v-model="content" type="textarea" :rows="7" placeholder="输入或粘贴 Markdown / 纯文本内容" />
            </div>

            <label class="knowledge-field" for="knowledge-title">资料标题</label>
            <el-input id="knowledge-title" v-model="title" maxlength="200" placeholder="例如：虚构项目说明" />

            <label class="knowledge-field" for="knowledge-visibility">可见范围</label>
            <el-select id="knowledge-visibility" v-model="visibility" style="width: 100%">
              <el-option label="仅自己" value="private" />
              <el-option label="同租户共享" value="tenant" />
            </el-select>

            <div class="knowledge-consent">
              <el-checkbox v-model="isSensitive">此资料包含敏感内容</el-checkbox>
              <p>敏感资料可以保存，但首期不能建立外部索引或启用。上传时不需要外发授权。</p>
            </div>
            <el-alert v-if="importError" class="knowledge-form-error" :title="importError" type="error" :closable="false" show-icon />
            <el-button class="knowledge-import-button" type="primary" :disabled="!canImport" :loading="uploading" @click="importDocument">保存资料</el-button>
          </div>
        </section>

        <section class="card knowledge-history" aria-label="历史资料">
          <div class="knowledge-card-head knowledge-history-head">
            <div>
              <h2>历史资料</h2>
              <p>最近 100 条可访问资料；索引和启用状态分别展示，操作仅限所有者。</p>
            </div>
            <span class="knowledge-count">{{ documents.length }} 项</span>
          </div>
          <div class="knowledge-history-toolbar">
            <el-input v-model="search" clearable placeholder="搜索资料标题" aria-label="搜索资料标题" />
            <el-select v-model="statusFilter" aria-label="筛选索引状态" style="width: 150px">
              <el-option label="全部状态" value="all" />
              <el-option label="已向量化" value="ready" />
              <el-option label="向量化中" value="running" />
              <el-option label="排队中" value="queued" />
              <el-option label="待索引" value="not_indexed" />
              <el-option label="索引失败" value="failed" />
              <el-option label="已取消" value="cancelled" />
              <el-option label="资料不可用" value="unavailable" />
            </el-select>
            <el-button :loading="loading" @click="loadDocuments()">刷新</el-button>
          </div>
          <div v-loading="loading" class="knowledge-history-scroll">
            <el-alert v-if="listError" :title="listError" type="error" :closable="false" show-icon />
            <article v-for="document in filteredDocuments" :key="document.document_id" class="knowledge-document">
              <div class="knowledge-document-top">
                <div class="knowledge-document-title">
                  <span class="knowledge-document-icon" aria-hidden="true">▤</span>
                  <div>
                    <h3>{{ document.title }}</h3>
                    <p>{{ document.visibility === 'tenant' ? '同租户共享' : '仅自己' }} · {{ formatTime(document.created_at) }}</p>
                  </div>
                </div>
                <div class="knowledge-document-statuses">
                  <span class="knowledge-status" :class="document.index_status">{{ statusLabel(document.index_status) }}</span>
                  <span class="knowledge-status" :class="document.enabled ? 'enabled' : 'disabled'">{{ document.enabled ? '已启用' : '未启用' }}</span>
                  <span v-if="document.is_sensitive" class="knowledge-status sensitive">敏感资料</span>
                </div>
              </div>
              <div v-if="document.job_id && ['queued', 'running'].includes(document.index_status)" class="knowledge-job-progress">
                <span>索引进度：{{ jobs[document.job_id]?.processed_chunks ?? 0 }} / {{ jobs[document.job_id]?.total_chunks ?? '—' }} 片段</span>
                <el-progress v-if="jobs[document.job_id]?.total_chunks" :percentage="Math.min(100, Math.round((jobs[document.job_id].processed_chunks / jobs[document.job_id].total_chunks) * 100))" :show-text="false" />
              </div>
              <p v-if="document.index_status === 'failed' && jobs[document.job_id]?.error_code" class="knowledge-job-error">失败原因：{{ jobs[document.job_id].error_code }}</p>
              <p v-if="document.job_id && jobErrors[document.job_id]" class="knowledge-job-error">{{ jobErrors[document.job_id] }}</p>
              <p v-if="actionErrors[document.document_id]" class="knowledge-job-error">{{ actionErrors[document.document_id] }}</p>
              <div class="knowledge-document-bottom">
                <el-button size="small" @click="showDetail(document)">详情</el-button>
                <span v-if="!document.can_manage" class="knowledge-shared-notice">共享资料 · 仅所有者可管理</span>
                <div v-else class="knowledge-document-actions">
                  <el-button v-if="document.index_status === 'failed' && document.job_id" size="small" :loading="busyDocumentId === document.document_id" @click="retryIndex(document)">重试索引</el-button>
                  <el-button v-else size="small" :disabled="document.index_status !== 'not_indexed' || document.is_sensitive || busyDocumentId === document.document_id" :loading="busyDocumentId === document.document_id" @click="confirmIndex(document)">{{ document.index_status === 'ready' ? '已向量化' : ['queued', 'running'].includes(document.index_status) ? '索引中' : '建立索引' }}</el-button>
                  <el-button size="small" :type="document.enabled ? 'danger' : 'primary'" plain :disabled="(!document.enabled && (document.index_status !== 'ready' || document.is_sensitive)) || busyDocumentId === document.document_id" :loading="busyDocumentId === document.document_id" @click="toggleEnabled(document)">{{ document.enabled ? '禁用资料' : '启用资料' }}</el-button>
                  <el-button size="small" :disabled="busyDocumentId === document.document_id" @click="shareDocument(document)">{{ document.visibility === 'tenant' ? '收回共享' : '共享' }}</el-button>
                  <el-button size="small" type="danger" plain :disabled="busyDocumentId === document.document_id" @click="removeDocument(document)">删除</el-button>
                </div>
              </div>
            </article>
            <p v-if="!loading && !listError && !filteredDocuments.length" class="knowledge-empty">暂无符合条件的资料。</p>
            <div class="knowledge-pages"><el-button :disabled="offset === 0 || loading" @click="changePage(-1)">上一页</el-button><span>第 {{ offset / 100 + 1 }} 页 · 筛选仅作用于当前页</span><el-button :disabled="documents.length < 100 || loading" @click="changePage(1)">下一页</el-button></div>
          </div>
        </section>
      </div>
    </section>
  </main>
</template>

<style scoped>
.knowledge-pages { display:flex;gap:12px;justify-content:center;align-items:center;font-size:12px;padding:16px; }
.knowledge-content { min-width: 0; }
.knowledge-notice { flex: none; margin-bottom: 18px; }
.knowledge-layout { display: grid; min-height: 0; flex: 1; grid-template-columns: minmax(310px, 360px) minmax(0, 1fr); gap: 18px; }
.knowledge-import, .knowledge-history { display: flex; min-height: 0; flex-direction: column; overflow: hidden; }
.knowledge-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex: none; padding: 19px 20px 15px; border-bottom: 1px solid #e4e9f0; }
.knowledge-card-head h2 { margin: 0 0 5px; color: #1f354d; font-size: 17px; }
.knowledge-card-head p { margin: 0; color: #667085; font-size: 12px; line-height: 1.5; }
.knowledge-import-body { min-height: 0; padding: 18px 20px 20px; overflow-y: auto; }
.knowledge-mode-switch { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; margin-bottom: 16px; padding: 4px; border-radius: 9px; background: #f1f4f8; }
.knowledge-mode-switch button { padding: 8px; border: 0; border-radius: 6px; background: transparent; color: #526176; font-weight: 650; }
.knowledge-mode-switch button.active { background: #fff; color: #1c5ba7; box-shadow: 0 1px 4px rgb(20 37 60 / 12%); }
.knowledge-dropzone { display: grid; justify-items: center; gap: 8px; padding: 20px 12px; border: 1px dashed #b9cce5; border-radius: 10px; background: #f8fbff; text-align: center; }
.knowledge-dropzone b { max-width: 100%; overflow-wrap: anywhere; }
.knowledge-dropzone p { margin: 0; color: #667085; font-size: 11px; line-height: 1.5; }
.knowledge-file-icon { display: grid; width: 42px; height: 42px; place-items: center; border-radius: 10px; background: #eaf3ff; color: #266dcc; font-size: 24px; }
.knowledge-file-input { display: none; }
.knowledge-text-entry { margin-bottom: 14px; }
.knowledge-text-entry label, .knowledge-field { display: block; margin: 16px 0 7px; color: #344054; font-size: 12px; font-weight: 700; }
.knowledge-text-entry label { margin-top: 0; }
.knowledge-consent { display: grid; gap: 4px; margin: 18px 0; }
.knowledge-consent :deep(.el-checkbox) { height: auto; align-items: flex-start; white-space: normal; }
.knowledge-consent p { margin: 5px 0 0; color: #667085; font-size: 11px; line-height: 1.5; }
.knowledge-form-error { margin-bottom: 12px; }
.knowledge-import-button { width: 100%; }
.knowledge-history-head { align-items: center; }
.knowledge-count { flex: none; padding: 5px 9px; border-radius: 20px; background: #eef4fc; color: #1c5ba7; font-size: 12px; }
.knowledge-history-toolbar { display: flex; gap: 10px; flex: none; padding: 14px 20px; border-bottom: 1px solid #e4e9f0; }
.knowledge-history-toolbar .el-input { min-width: 90px; }
.knowledge-history-toolbar .el-button { flex: none; margin-left: 0; }
.knowledge-history-scroll { min-height: 0; flex: 1; padding: 5px 20px 20px; overflow-y: auto; }
.knowledge-document { padding: 16px 0; border-bottom: 1px solid #e4e9f0; }
.knowledge-document:last-child { border-bottom: 0; }
.knowledge-document-top, .knowledge-document-bottom { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.knowledge-document-title { display: flex; min-width: 0; gap: 11px; }
.knowledge-document-icon { display: grid; width: 34px; height: 34px; flex: none; place-items: center; border-radius: 8px; background: #edf4fd; color: #266dcc; font-size: 20px; }
.knowledge-document h3 { margin: 0 0 5px; overflow-wrap: anywhere; color: #1f354d; font-size: 14px; }
.knowledge-document p { margin: 0; color: #667085; font-size: 11px; line-height: 1.5; }
.knowledge-document-statuses { display: flex; flex: none; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.knowledge-status { flex: none; padding: 4px 8px; border-radius: 20px; background: #f1f4f8; color: #526176; font-size: 11px; }
.knowledge-status.ready { background: #e8f7ef; color: #17795d; }
.knowledge-status.queued, .knowledge-status.running { background: #eaf3ff; color: #1c5ba7; }
.knowledge-status.failed { background: #fff0f0; color: #b42318; }
.knowledge-status.enabled { background: #edf0ff; color: #5448a5; }
.knowledge-status.sensitive { background: #fff0f7; color: #a22d68; }
.knowledge-job-progress { display: grid; gap: 6px; margin: 12px 0 0 45px; color: #526176; font-size: 11px; }
.knowledge-job-progress .el-progress { max-width: 300px; }
.knowledge-job-error { margin: 10px 0 0 45px !important; color: #b42318 !important; }
.knowledge-document-bottom { align-items: center; margin: 12px 0 0 45px; }
.knowledge-shared-notice { color: #667085; font-size: 11px; }
.knowledge-document-actions { display: flex; flex-wrap: wrap; gap: 7px; margin-left: auto; }
.knowledge-document-actions :deep(.el-button + .el-button) { margin-left: 0; }
.knowledge-empty { padding: 30px 0; color: #667085; text-align: center; }
@media (max-width: 1050px) { .knowledge-layout { grid-template-columns: minmax(0, 1fr); overflow-y: auto; }.knowledge-import, .knowledge-history { min-height: 350px; }.knowledge-import-body, .knowledge-history-scroll { overflow: visible; } }
@media (max-width: 800px) { .knowledge-content { height: auto; overflow: visible; }.knowledge-layout { overflow: visible; }.knowledge-history-toolbar, .knowledge-document-bottom { flex-wrap: wrap; }.knowledge-document-bottom { margin-left: 0; } }
</style>
