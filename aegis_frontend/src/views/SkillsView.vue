<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import AppNavigation from '../components/AppNavigation.vue'
import { useAuthStore } from '../store/auth'
import {
  createSkill, deleteSkill, getRunSkillUsages, getSkill, getSkillVersion,
  listSkills, listSkillTemplates, listSkillVersions, publishSkill, setSkillEnabled, updateSkill,
} from '../api/skills'

const auth = useAuthStore()
const router = useRouter()
const purposeOptions = [
  { value: 'mail_reply_draft', label: '起草邮件回复' },
  { value: 'mail_draft_revision', label: '修改邮件草稿' },
]
const items = ref([])
const templates = ref([])
const listMode = ref('mine')
const search = ref('')
const statusFilter = ref('all')
const selectedId = ref(null)
const detail = ref(null)
const detailTab = ref('draft')
const listLoading = ref(false)
const templateLoading = ref(false)
const detailLoading = ref(false)
const listError = ref('')
const templateError = ref('')
const detailError = ref('')
const actionError = ref('')
const actionBusy = ref(false)
let detailRequest = 0

const versionItems = ref([])
const versionNextBefore = ref(null)
const versionDetails = ref({})
const selectedVersionNumber = ref(null)
const versionLoading = ref(false)
const versionError = ref('')
const dialogOpen = ref(false)
const editingId = ref(null)
const saving = ref(false)
const formError = ref('')
const form = reactive({ name: '', description: '', purposes: [], body: '' })

const usageOpen = ref(false)
const runIdInput = ref('')
const usageRunId = ref('')
const usageItems = ref([])
const usageBindings = ref([])
const usageCursor = ref(null)
const usageLoading = ref(false)
const usageError = ref('')

const enabledCount = computed(() => items.value.filter((skill) => skill.enabled).length)
const publishedCount = computed(() => items.value.filter((skill) => skill.published_version !== null).length)
const filteredItems = computed(() => {
  const term = search.value.trim().toLocaleLowerCase()
  return items.value.filter((skill) => {
    const textMatches = !term || [skill.name, skill.description].some((value) => value.toLocaleLowerCase().includes(term))
    const statusMatches = statusFilter.value === 'all'
      || (statusFilter.value === 'published' && skill.published_version !== null)
      || (statusFilter.value === 'draft' && skill.published_version === null)
      || (statusFilter.value === 'enabled' && skill.enabled)
    return textMatches && statusMatches
  })
})
const activeTemplate = computed(() => templates.value[0] ?? null)
const publishedDetail = computed(() => detail.value?.published_version
  ? versionDetails.value[detail.value.published_version] ?? null : null)
const historicalDetail = computed(() => selectedVersionNumber.value
  ? versionDetails.value[selectedVersionNumber.value] ?? null : null)

function purposeLabel(value) {
  return purposeOptions.find((item) => item.value === value)?.label ?? value
}
function statusLabel(skill) {
  if (skill.published_version === null) return '未发布草稿'
  return skill.enabled ? '已发布 · 启用' : '已发布 · 停用'
}
function formatTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  }).format(date)
}
function errorMessage(error, fallback = '操作失败，请稍后重试。') {
  const code = error?.response?.data?.detail?.code
  const messages = {
    SKILL_NOT_FOUND: 'Skill 不存在或无权访问，请刷新列表。',
    SKILL_VERSION_NOT_FOUND: '该历史版本不存在或无权访问。',
    RUN_NOT_FOUND: '任务不存在或无权访问。',
    SKILL_REVISION_CONFLICT: '此 Skill 已被其他操作修改。请先刷新详情、核对内容，再重新提交；不会自动覆盖。',
    SKILL_NAME_CONFLICT: '同名 Skill 已存在，请修改标识后重试。',
    SKILL_CATALOG_LIMIT: '个人 Skill 已达到 20 条上限。',
    SKILL_NOT_PUBLISHED: '未发布的 Skill 不能启用，请先发布。',
  }
  if (code && messages[code]) return messages[code]
  if (error?.response?.status === 422) return '输入不符合接口要求，请检查标识、描述、用途和正文长度。'
  if (error?.response?.status === 401) return '登录已失效，请重新登录。'
  return error?.message || fallback
}
function clearVersionState() {
  versionItems.value = []
  versionNextBefore.value = null
  versionDetails.value = {}
  selectedVersionNumber.value = null
  versionLoading.value = false
  versionError.value = ''
  detailTab.value = 'draft'
}
async function loadDetail(skillId) {
  const request = ++detailRequest
  selectedId.value = skillId
  detail.value = null
  detailError.value = ''
  actionError.value = ''
  clearVersionState()
  if (!skillId) return
  detailLoading.value = true
  try {
    const result = await getSkill(skillId)
    if (request === detailRequest) detail.value = result
  } catch (error) {
    if (request === detailRequest) detailError.value = errorMessage(error, 'Skill 详情读取失败。')
  } finally {
    if (request === detailRequest) detailLoading.value = false
  }
}
async function loadList({ keepSelection = true } = {}) {
  listLoading.value = true
  listError.value = ''
  try {
    const result = await listSkills()
    items.value = result.items ?? []
    const id = keepSelection && items.value.some((item) => item.id === selectedId.value)
      ? selectedId.value : items.value[0]?.id ?? null
    await loadDetail(id)
  } catch (error) {
    listError.value = errorMessage(error, 'Skill 列表读取失败。')
  } finally {
    listLoading.value = false
  }
}
async function loadTemplates() {
  templateLoading.value = true
  templateError.value = ''
  try {
    const result = await listSkillTemplates()
    templates.value = result.items ?? []
  } catch (error) {
    templateError.value = errorMessage(error, '内置模板读取失败。')
  } finally {
    templateLoading.value = false
  }
}
async function loadVersion(version) {
  const skillId = selectedId.value
  if (!skillId || versionDetails.value[version]) return
  versionLoading.value = true
  versionError.value = ''
  try {
    const result = await getSkillVersion(skillId, version)
    if (selectedId.value === skillId) versionDetails.value = { ...versionDetails.value, [version]: result }
  } catch (error) {
    if (selectedId.value === skillId) versionError.value = errorMessage(error, '历史版本读取失败。')
  } finally {
    if (selectedId.value === skillId) versionLoading.value = false
  }
}
async function loadVersions(more = false) {
  const skillId = selectedId.value
  if (!skillId || (more && !versionNextBefore.value)) return
  versionLoading.value = true
  versionError.value = ''
  try {
    const result = await listSkillVersions(skillId, more ? versionNextBefore.value : undefined)
    if (selectedId.value !== skillId) return
    versionItems.value = more ? [...versionItems.value, ...(result.items ?? [])] : result.items ?? []
    versionNextBefore.value = result.next_before ?? null
    if (!selectedVersionNumber.value && versionItems.value.length) selectedVersionNumber.value = versionItems.value[0].version
    if (selectedVersionNumber.value) await loadVersion(selectedVersionNumber.value)
  } catch (error) {
    if (selectedId.value === skillId) versionError.value = errorMessage(error, '版本列表读取失败。')
  } finally {
    if (selectedId.value === skillId) versionLoading.value = false
  }
}
async function openDetailTab(tab) {
  detailTab.value = tab
  if (tab === 'published' && detail.value?.published_version) await loadVersion(detail.value.published_version)
  if (tab === 'history' && !versionItems.value.length) await loadVersions()
}
async function chooseVersion(version) {
  selectedVersionNumber.value = version
  await loadVersion(version)
}
function openCreate(source = null) {
  editingId.value = null
  Object.assign(form, {
    name: source?.name ?? '', description: source?.description ?? '',
    purposes: source ? [...source.purposes] : [], body: source?.body ?? '',
  })
  formError.value = ''
  dialogOpen.value = true
}
function openEdit() {
  if (!detail.value) return
  editingId.value = detail.value.id
  Object.assign(form, {
    name: detail.value.name, description: detail.value.description,
    purposes: [...detail.value.purposes], body: detail.value.body,
  })
  formError.value = ''
  dialogOpen.value = true
}
async function saveSkill() {
  const name = form.name.trim()
  const description = form.description.trim()
  const body = form.body.trim()
  if (!/^[a-z][a-z0-9-]{0,63}$/.test(name)) {
    formError.value = '标识须以小写字母开头，只能包含小写字母、数字和连字符（最多 64 字符）。'
    return
  }
  if (!description || description.length > 300 || !body || body.length > 6000 || !form.purposes.length) {
    formError.value = '请填写 1–300 字描述、1–6000 字正文，并选择至少一项适用任务。'
    return
  }
  const payload = { name, description, body, purposes: [...form.purposes].sort() }
  saving.value = true
  formError.value = ''
  try {
    const result = editingId.value
      ? await updateSkill(editingId.value, { ...payload, expected_revision: detail.value.revision })
      : await createSkill(payload)
    dialogOpen.value = false
    selectedId.value = result.id
    listMode.value = 'mine'
    search.value = ''
    statusFilter.value = 'all'
    await loadList()
    ElMessage.success('草稿已保存；发布前不会供新任务使用。')
  } catch (error) {
    formError.value = errorMessage(error, '保存草稿失败。')
  } finally {
    saving.value = false
  }
}
async function publishCurrent() {
  if (!detail.value) return
  try {
    await ElMessageBox.confirm('发布将当前草稿保存为不可变版本并启用。之后的新邮件任务可能选用它；不会自动发送邮件。请确认内容已核对。', '发布 Skill', {
      confirmButtonText: '确认发布', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  actionBusy.value = true
  actionError.value = ''
  try {
    await publishSkill(detail.value.id, detail.value.revision)
    await loadList()
    ElMessage.success('已发布并启用。')
  } catch (error) {
    actionError.value = errorMessage(error, '发布失败。')
  } finally {
    actionBusy.value = false
  }
}
async function toggleCurrent() {
  if (!detail.value?.published_version) return
  const enabled = !detail.value.enabled
  try {
    await ElMessageBox.confirm(enabled ? '启用后，新邮件任务可能选用当前已发布版本。' : '停用后，后续模型调用将不能继续加载此 Skill；已发出的请求无法撤销。', enabled ? '启用 Skill' : '停用 Skill', {
      confirmButtonText: enabled ? '确认启用' : '确认停用', cancelButtonText: '取消',
    })
  } catch { return }
  actionBusy.value = true
  actionError.value = ''
  try {
    await setSkillEnabled(detail.value.id, detail.value.revision, enabled)
    await loadList()
    ElMessage.success(enabled ? '已启用。' : '已停用。')
  } catch (error) {
    actionError.value = errorMessage(error, '状态变更失败。')
  } finally {
    actionBusy.value = false
  }
}
async function removeCurrent() {
  if (!detail.value) return
  try {
    await ElMessageBox.confirm('删除「' + detail.value.name + '」后将停止后续加载，并从普通列表移除。历史版本和使用审计仍会保留，此操作不能在页面上撤销。', '确认删除 Skill', {
      confirmButtonText: '确认删除', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  actionBusy.value = true
  actionError.value = ''
  try {
    await deleteSkill(detail.value.id, detail.value.revision)
    selectedId.value = null
    await loadList({ keepSelection: false })
    ElMessage.success('Skill 已删除。')
  } catch (error) {
    actionError.value = errorMessage(error, '删除失败。')
  } finally {
    actionBusy.value = false
  }
}
async function loadUsages(more = false) {
  const runId = more ? usageRunId.value : runIdInput.value.trim()
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(runId)) {
    usageError.value = '请输入有效的任务 ID（UUID）。'
    return
  }
  usageLoading.value = true
  usageError.value = ''
  try {
    const result = await getRunSkillUsages(runId, more ? usageCursor.value : undefined)
    if (!more) {
      usageRunId.value = runId
      usageItems.value = []
    }
    usageBindings.value = result.bindings ?? []
    usageItems.value = [...usageItems.value, ...(result.items ?? [])]
    usageCursor.value = result.next_cursor ?? null
  } catch (error) {
    usageError.value = errorMessage(error, '使用记录读取失败。')
  } finally {
    usageLoading.value = false
  }
}
function openUsages() {
  usageOpen.value = true
  usageError.value = ''
}
async function signOut() {
  await auth.signOut()
  await router.replace({ name: 'login' })
}
onMounted(() => {
  loadList()
  loadTemplates()
})
</script>

<template>
  <main class="workspace">
    <aside class="sidebar">
      <div class="brand">Aegis PA</div>
      <AppNavigation active="skills" />
      <div class="identity">
        <b>{{ auth.profile?.name }}</b>
        <span>{{ auth.profile?.email || '已通过 Keycloak 登录' }}</span>
        <el-button text type="primary" @click="signOut">退出登录</el-button>
      </div>
    </aside>

    <section class="content skills-page">
      <header class="page-header skills-header">
        <div><h1>Skill 管理</h1><p class="muted">管理邮件草稿任务使用的个人指南；保存、发布与启用是不同操作。</p></div>
        <div class="skills-header-actions">
          <el-button @click="openUsages">查看任务使用记录</el-button>
          <el-button type="primary" :disabled="items.length >= 20" @click="openCreate()">+ 新建 Skill</el-button>
        </div>
      </header>

      <div class="skills-notice" role="note">
        <strong>使用边界</strong>
        <span>Skill 是低优先级的处理指南，不新增工具权限，也不会自动发送邮件。请勿录入密码、密钥或不应外发的资料。当前仅适用于邮件回复起草与草稿修改。</span>
      </div>
      <div class="skills-summary" aria-label="Skill 概览">
        <div><span>个人 Skill</span><b>{{ items.length }} / 20</b></div>
        <div><span>已发布</span><b>{{ publishedCount }}</b></div>
        <div><span>已启用</span><b>{{ enabledCount }}</b></div>
      </div>

      <div class="skills-layout">
        <section class="card skills-library" aria-label="Skill 列表">
          <div class="skills-switcher" role="tablist" aria-label="Skill 来源">
            <button type="button" role="tab" :aria-selected="listMode === 'mine'" :class="{ active: listMode === 'mine' }" @click="listMode = 'mine'">我的 Skill</button>
            <button type="button" role="tab" :aria-selected="listMode === 'template'" :class="{ active: listMode === 'template' }" @click="listMode = 'template'">内置模板 <span>只读</span></button>
          </div>
          <template v-if="listMode === 'mine'">
            <div class="skills-filters">
              <el-input v-model="search" clearable placeholder="搜索名称或用途" aria-label="搜索 Skill" />
              <el-select v-model="statusFilter" aria-label="筛选 Skill 状态">
                <el-option label="全部状态" value="all" /><el-option label="已发布" value="published" />
                <el-option label="未发布" value="draft" /><el-option label="已启用" value="enabled" />
              </el-select>
            </div>
            <p v-if="listError" class="skills-inline-error" role="alert">{{ listError }} <el-button link type="primary" @click="loadList()">重试</el-button></p>
            <div v-loading="listLoading" class="skills-list">
              <button v-for="skill in filteredItems" :key="skill.id" type="button" class="skill-row"
                :class="{ selected: selectedId === skill.id }" :aria-pressed="selectedId === skill.id" @click="loadDetail(skill.id)">
                <span class="skill-row-top"><strong>{{ skill.name }}</strong><span class="skill-state" :class="{ enabled: skill.enabled }">{{ statusLabel(skill) }}</span></span>
                <span class="skill-row-desc">{{ skill.description }}</span>
                <span class="skill-row-meta">修订 {{ skill.revision }} · {{ skill.purposes.map(purposeLabel).join('、') }}</span>
                <span class="skill-row-meta">更新于 {{ formatTime(skill.updated_at) }}</span>
              </button>
              <div v-if="!listLoading && !filteredItems.length && !listError" class="skills-empty">
                <strong>{{ items.length ? '没有匹配的 Skill' : '还没有个人 Skill' }}</strong>
                <span>{{ items.length ? '调整搜索或筛选条件。' : '可从内置模板复制，或新建草稿。' }}</span>
              </div>
            </div>
          </template>
          <div v-else v-loading="templateLoading" class="skills-list">
            <p v-if="templateError" class="skills-inline-error" role="alert">{{ templateError }} <el-button link type="primary" @click="loadTemplates">重试</el-button></p>
            <div v-for="template in templates" :key="template.name" class="template-row"><strong>{{ template.name }}</strong><span>{{ template.description }}</span></div>
            <div v-if="!templateLoading && !templates.length && !templateError" class="skills-empty">暂无内置模板。</div>
          </div>
        </section>

        <section class="card skills-detail" aria-label="Skill 详情">
          <template v-if="listMode === 'template' && activeTemplate">
            <div class="skills-detail-head">
              <div><span class="skills-kicker">内置模板 · 只读</span><h2>{{ activeTemplate.name }}</h2><p>{{ activeTemplate.description }}</p></div>
              <span class="skill-state">只读</span>
            </div>
            <div class="skills-detail-scroll">
              <div class="skills-block"><h3>适用任务</h3><div class="skills-purposes"><span v-for="purpose in activeTemplate.purposes" :key="purpose">{{ purposeLabel(purpose) }}</span></div></div>
              <div class="skills-block"><h3>模板正文</h3><pre class="skills-body">{{ activeTemplate.body }}</pre></div>
              <div class="skills-runtime-note">模板不会自动启用。复制为个人草稿并核对后，才可发布。</div>
            </div>
            <div class="skills-actions"><el-button type="primary" :disabled="items.length >= 20" @click="openCreate(activeTemplate)">以模板新建草稿</el-button></div>
          </template>
          <template v-else-if="listMode === 'mine' && detail">
            <div class="skills-detail-head">
              <div><span class="skills-kicker">个人 Skill</span><h2>{{ detail.name }}</h2><p>{{ detail.description }}</p></div>
              <span class="skill-state" :class="{ enabled: detail.enabled }">{{ statusLabel(detail) }}</span>
            </div>
            <div class="skills-detail-tabs" role="tablist" aria-label="Skill 内容">
              <button type="button" role="tab" :aria-selected="detailTab === 'draft'" :class="{ active: detailTab === 'draft' }" @click="openDetailTab('draft')">当前草稿</button>
              <button type="button" role="tab" :aria-selected="detailTab === 'published'" :class="{ active: detailTab === 'published' }" :disabled="!detail.published_version" @click="openDetailTab('published')">已发布版本</button>
              <button type="button" role="tab" :aria-selected="detailTab === 'history'" :class="{ active: detailTab === 'history' }" :disabled="!detail.published_version" @click="openDetailTab('history')">历史版本</button>
            </div>
            <div class="skills-detail-scroll" v-loading="versionLoading">
              <template v-if="detailTab === 'draft'">
                <div class="skills-facts"><div><span>草稿修订号</span><strong>{{ detail.revision }}</strong></div><div><span>当前发布版本</span><strong>{{ detail.published_version ? 'v' + detail.published_version : '尚未发布' }}</strong></div></div>
                <p v-if="detail.published_version" class="skills-pending-note">编辑只保存草稿；发布版是否与草稿相同，请切换“已发布版本”核对。不能仅用修订号判断。</p>
                <div class="skills-block"><h3>适用任务</h3><div class="skills-purposes"><span v-for="purpose in detail.purposes" :key="purpose">{{ purposeLabel(purpose) }}</span></div></div>
                <div class="skills-block"><h3>Markdown 正文</h3><pre class="skills-body">{{ detail.body }}</pre></div>
              </template>
              <template v-else-if="detailTab === 'published'">
                <p v-if="versionError" class="skills-inline-error" role="alert">{{ versionError }} <el-button link type="primary" @click="loadVersion(detail.published_version)">重试</el-button></p>
                <template v-if="publishedDetail">
                  <div class="skills-facts"><div><span>发布版本</span><strong>v{{ publishedDetail.version }}</strong></div><div><span>发布时间</span><strong>{{ formatTime(publishedDetail.created_at) }}</strong></div></div>
                  <div class="skills-block"><h3>适用任务</h3><div class="skills-purposes"><span v-for="purpose in publishedDetail.purposes" :key="purpose">{{ purposeLabel(purpose) }}</span></div></div>
                  <div class="skills-block"><h3>不可变版本正文</h3><pre class="skills-body">{{ publishedDetail.body }}</pre></div>
                </template>
              </template>
              <template v-else>
                <p v-if="versionError" class="skills-inline-error" role="alert">{{ versionError }} <el-button link type="primary" @click="versionItems.length ? loadVersion(selectedVersionNumber) : loadVersions()">重试</el-button></p>
                <div class="skills-version-list">
                  <button v-for="version in versionItems" :key="version.version" type="button" :class="{ active: selectedVersionNumber === version.version }" @click="chooseVersion(version.version)">
                    <strong>v{{ version.version }}</strong><span>{{ formatTime(version.created_at) }}</span>
                  </button>
                </div>
                <el-button v-if="versionNextBefore" class="skills-more" @click="loadVersions(true)">加载更多历史版本</el-button>
                <div v-if="historicalDetail" class="skills-block"><h3>v{{ historicalDetail.version }} 正文（只读）</h3><p class="muted">{{ historicalDetail.description }}</p><pre class="skills-body">{{ historicalDetail.body }}</pre></div>
              </template>
              <div class="skills-runtime-note">Skill 不授予工具权限，不会自动发送邮件。停用或删除可阻止后续加载，但无法撤销已发出的模型请求。</div>
            </div>
            <p v-if="actionError" class="skills-inline-error skills-action-error" role="alert">{{ actionError }} <el-button link type="primary" @click="loadDetail(selectedId)">刷新详情</el-button></p>
            <div class="skills-actions">
              <el-button type="primary" plain :disabled="actionBusy" @click="openEdit">编辑草稿</el-button>
              <el-button type="primary" :loading="actionBusy" @click="publishCurrent">{{ detail.published_version ? '发布新版本' : '发布并启用' }}</el-button>
              <el-button :disabled="!detail.published_version || actionBusy" @click="toggleCurrent">{{ detail.enabled ? '停用' : '启用' }}</el-button>
              <el-button type="danger" plain :disabled="actionBusy" @click="removeCurrent">删除</el-button>
            </div>
          </template>
          <div v-else-if="listMode === 'mine'" v-loading="detailLoading" class="skills-detail-empty">
            <p v-if="detailError" role="alert">{{ detailError }} <el-button link type="primary" @click="loadDetail(selectedId)">重试</el-button></p>
            <span v-else>{{ detailLoading ? '正在读取 Skill 详情…' : '选择左侧 Skill 查看详情，或新建草稿。' }}</span>
          </div>
          <div v-else class="skills-detail-empty">选择内置模板查看详情。</div>
        </section>
      </div>
    </section>

    <el-dialog v-model="dialogOpen" :title="editingId ? '编辑 Skill 草稿' : '新建个人 Skill 草稿'" width="min(680px, calc(100vw - 32px))" append-to-body>
      <p class="skills-dialog-note">保存不会发布。正文将成为模型的处理指南，请勿录入密码、密钥或不应外发的资料。预览以纯文本显示，不执行 Markdown 中的 HTML。</p>
      <el-form label-position="top" @submit.prevent="saveSkill">
        <el-form-item label="标识名称" required><el-input v-model="form.name" maxlength="64" show-word-limit placeholder="例如：project-status-reply" /></el-form-item>
        <el-form-item label="用途描述" required><el-input v-model="form.description" type="textarea" :rows="2" maxlength="300" show-word-limit placeholder="说明何时适用、何时不适用" /></el-form-item>
        <el-form-item label="适用任务" required><el-checkbox-group v-model="form.purposes"><el-checkbox v-for="purpose in purposeOptions" :key="purpose.value" :value="purpose.value">{{ purpose.label }}</el-checkbox></el-checkbox-group></el-form-item>
        <el-form-item label="指南正文（Markdown）" required><el-input v-model="form.body" type="textarea" :rows="9" maxlength="6000" show-word-limit placeholder="写处理步骤、写作结构和边界；不要写工具权限或脚本" /></el-form-item>
        <p v-if="formError" class="skills-form-error" role="alert">{{ formError }}</p>
      </el-form>
      <template #footer><el-button :disabled="saving" @click="dialogOpen = false">取消</el-button><el-button v-if="formError.includes('其他操作修改')" @click="dialogOpen = false; loadDetail(selectedId)">关闭并刷新详情</el-button><el-button type="primary" :loading="saving" @click="saveSkill">保存草稿</el-button></template>
    </el-dialog>

    <el-dialog v-model="usageOpen" title="任务使用的 Skill" width="min(760px, calc(100vw - 32px))" append-to-body>
      <p class="skills-dialog-note">按任务 ID 查询真实绑定与注入记录。没有记录也可能是旧策略或未执行适用任务，不代表加载失败。</p>
      <div class="skills-usage-search"><el-input v-model="runIdInput" placeholder="输入任务 ID（UUID）" aria-label="任务 ID" @keyup.enter="loadUsages()" /><el-button type="primary" :loading="usageLoading" @click="loadUsages()">查询</el-button></div>
      <p v-if="usageError" class="skills-form-error" role="alert">{{ usageError }}</p>
      <template v-if="usageRunId && !usageError">
        <h3 class="skills-usage-heading">选择与绑定</h3>
        <div v-if="!usageBindings.length" class="skills-usage-empty">没有绑定记录；可能是旧策略或未执行适用用途。</div>
        <div v-for="(binding, index) in usageBindings" :key="index" class="skills-usage-item">
          <strong>{{ purposeLabel(binding.purpose) }} · {{ binding.status }}</strong>
          <span>{{ binding.skill_id ? 'Skill ' + binding.skill_id + ' · v' + binding.version : '未选择 Skill' }} · {{ binding.reason || '原因未知' }} · 读取 {{ binding.reads ?? '未知' }} 次</span>
        </div>
        <h3 class="skills-usage-heading">实际注入</h3>
        <div v-if="!usageItems.length" class="skills-usage-empty">没有实际注入记录。</div>
        <div v-for="usage in usageItems" :key="usage.attempt_id" class="skills-usage-item">
          <strong>{{ purposeLabel(usage.purpose) }} · v{{ usage.version }} · {{ usage.status }}</strong>
          <span>Skill {{ usage.skill_id }} · {{ formatTime(usage.created_at) }} · 正文 {{ usage.loaded_chars }} 字符</span>
          <span>整次调用 Token：输入 {{ usage.prompt_tokens ?? '未知' }} / 输出 {{ usage.completion_tokens ?? '未知' }} / 总计 {{ usage.total_tokens ?? '未知' }}；耗时 {{ usage.duration_ms == null ? '未知' : Math.round(usage.duration_ms) + ' ms' }}</span>
        </div>
        <el-button v-if="usageCursor" class="skills-more" :loading="usageLoading" @click="loadUsages(true)">加载更多记录</el-button>
      </template>
      <template #footer><el-button @click="usageOpen = false">关闭</el-button></template>
    </el-dialog>
  </main>
</template>

<style scoped>
.skills-page { min-width: 0; }
.skills-header { align-items: center; margin-bottom: 18px; }
.skills-header .muted { margin-bottom: 0; }
.skills-header-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.skills-header-actions .el-button { margin: 0; }
.skills-notice { display: flex; gap: 9px; flex: none; padding: 12px 15px; border: 1px solid #cbdff8; border-radius: 9px; background: #edf5ff; color: #365777; font-size: 13px; line-height: 1.5; }
.skills-notice strong { flex: none; color: #1c5ba7; }
.skills-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; flex: none; margin: 16px 0; }
.skills-summary > div { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 13px 16px; border: 1px solid #dfe5ee; border-radius: 10px; background: #fff; }
.skills-summary span { color: #667085; font-size: 12px; }
.skills-summary b { color: #173f66; font-size: 22px; }
.skills-layout { display: grid; grid-template-columns: minmax(320px, .92fr) minmax(0, 1.35fr); gap: 16px; min-height: 0; flex: 1; }
.skills-library, .skills-detail { display: flex; min-width: 0; min-height: 0; flex-direction: column; overflow: hidden; }
.skills-switcher, .skills-detail-tabs { display: flex; gap: 12px; flex: none; padding: 0 18px; border-bottom: 1px solid #e6edf5; }
.skills-switcher button, .skills-detail-tabs button { padding: 17px 3px 13px; border: 0; border-bottom: 2px solid transparent; background: none; color: #667085; font-weight: 700; }
.skills-switcher button.active, .skills-detail-tabs button.active { border-bottom-color: #266dcc; color: #1c5ba7; }
.skills-detail-tabs button:disabled { opacity: .45; cursor: not-allowed; }
.skills-switcher span { margin-left: 3px; color: #98a2b3; font-size: 11px; }
.skills-filters { display: flex; gap: 8px; flex: none; padding: 15px 18px; border-bottom: 1px solid #e6edf5; }
.skills-filters .el-input { min-width: 0; flex: 1; }
.skills-filters .el-select { width: 115px; flex: none; }
.skills-list { min-height: 0; flex: 1; overflow-y: auto; padding: 9px; }
.skill-row { display: grid; width: 100%; gap: 8px; padding: 15px; border: 1px solid transparent; border-radius: 9px; background: #fff; color: #243952; text-align: left; }
.skill-row + .skill-row { margin-top: 5px; }
.skill-row:hover, .skill-row.selected { border-color: #bdd6f6; background: #eff6ff; }
.skill-row:focus-visible, .skills-switcher button:focus-visible, .skills-detail-tabs button:focus-visible { outline: 2px solid #266dcc; outline-offset: 2px; }
.skill-row-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.skill-row-top strong { font-size: 14px; overflow-wrap: anywhere; }
.skill-state { display: inline-flex; align-items: center; flex: none; padding: 4px 8px; border-radius: 99px; background: #f1f3f7; color: #667085; font-size: 11px; font-weight: 700; white-space: nowrap; }
.skill-state.enabled { background: #e8f7ef; color: #17795d; }
.skill-row-desc { color: #526176; font-size: 12px; line-height: 1.5; }
.skill-row-meta { color: #8290a2; font-size: 11px; line-height: 1.5; }
.template-row { display: grid; gap: 7px; padding: 15px; border-radius: 9px; background: #eff6ff; color: #243952; }
.template-row span { color: #667085; font-size: 12px; }
.skills-empty, .skills-detail-empty { display: grid; gap: 6px; place-content: center; min-height: 150px; padding: 22px; color: #667085; font-size: 13px; text-align: center; }
.skills-empty strong { color: #344054; }
.skills-detail-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex: none; padding: 22px 24px; border-bottom: 1px solid #e6edf5; }
.skills-kicker { display: block; margin-bottom: 8px; color: #4676aa; font-size: 11px; font-weight: 800; letter-spacing: .06em; }
.skills-detail-head h2 { margin: 0 0 5px; color: #172b49; font-size: 21px; overflow-wrap: anywhere; }
.skills-detail-head p { margin: 0; color: #526176; line-height: 1.5; }
.skills-detail-scroll { min-height: 0; flex: 1; overflow-y: auto; padding: 21px 24px; }
.skills-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.skills-facts > div { display: grid; gap: 6px; padding: 12px 14px; border-radius: 8px; background: #f7f9fc; }
.skills-facts span { color: #667085; font-size: 11px; }
.skills-facts strong { color: #344054; font-size: 13px; }
.skills-block { margin-top: 23px; }
.skills-block h3 { margin-bottom: 10px; color: #243952; font-size: 14px; }
.skills-purposes { display: flex; flex-wrap: wrap; gap: 8px; }
.skills-purposes span { padding: 6px 10px; border: 1px solid #d4e3f7; border-radius: 6px; background: #f5f9ff; color: #2b5c96; font-size: 12px; }
.skills-body { margin: 0; padding: 15px; border: 1px solid #e3eaf3; border-radius: 8px; background: #fafcff; color: #344054; font: inherit; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.skills-pending-note { padding: 10px 12px; margin: 14px 0 0; border-radius: 7px; background: #fff5e8; color: #92550b; font-size: 12px; line-height: 1.5; }
.skills-version-list { display: flex; flex-wrap: wrap; gap: 8px; }
.skills-version-list button { padding: 9px 11px; border: 1px solid #dce5f0; border-radius: 7px; background: white; color: #344054; }
.skills-version-list button.active { border-color: #91b9eb; background: #eff6ff; }
.skills-version-list span { display: block; margin-top: 3px; color: #667085; font-size: 11px; }
.skills-more { margin-top: 12px; }
.skills-runtime-note { margin-top: 25px; padding: 13px 15px; border-left: 3px solid #91b6e7; border-radius: 4px; background: #f4f8fd; color: #526176; font-size: 12px; line-height: 1.55; }
.skills-actions { display: flex; flex-wrap: wrap; gap: 8px; flex: none; padding: 16px 24px; border-top: 1px solid #e6edf5; }
.skills-actions .el-button { margin: 0; }
.skills-inline-error { padding: 9px 14px; margin: 8px; border-radius: 7px; background: #fff0f0; color: #aa3535; font-size: 12px; line-height: 1.5; }
.skills-action-error { flex: none; }
.skills-dialog-note { margin: 0 0 17px; color: #667085; font-size: 12px; line-height: 1.5; }
.skills-form-error { margin: 8px 0; color: #bd3c3c; font-size: 12px; }
.skills-usage-search { display: flex; gap: 8px; }
.skills-usage-search .el-input { min-width: 0; flex: 1; }
.skills-usage-heading { margin: 20px 0 8px; font-size: 14px; }
.skills-usage-item { display: grid; gap: 5px; padding: 11px 13px; margin-bottom: 7px; border: 1px solid #e3eaf3; border-radius: 8px; color: #344054; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
.skills-usage-item span, .skills-usage-empty { color: #667085; }
.skills-usage-empty { padding: 12px; border-radius: 8px; background: #f7f9fc; font-size: 12px; }
@media (max-width: 1050px) { .skills-layout { grid-template-columns: 1fr; overflow-y: auto; } .skills-library { min-height: 270px; max-height: 340px; } .skills-detail { min-height: 380px; } }
@media (max-width: 800px) { .skills-page { height: auto; } .skills-layout { overflow: visible; } .skills-library { max-height: 360px; } .skills-detail { min-height: 0; } .skills-detail-scroll { overflow: visible; } }
@media (max-width: 560px) { .skills-header { align-items: flex-start; flex-direction: column; } .skills-notice { flex-direction: column; gap: 3px; } .skills-summary { gap: 6px; } .skills-summary > div { align-items: flex-start; flex-direction: column; } .skills-filters, .skills-usage-search { flex-direction: column; } .skills-filters .el-select { width: 100%; } .skills-facts { grid-template-columns: 1fr; } }
</style>
