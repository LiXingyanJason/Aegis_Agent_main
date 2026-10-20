<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { getRunMailToolResults } from '../api/run'

const props = defineProps({
  runId: { type: String, required: true },
  toolCallId: { type: String, required: true },
})
const emit = defineEmits(['close'])
const items = ref([])
const total = ref(0)
const matchedCount = ref(null)
const queryAction = ref('')
const source = ref('')
const loading = ref(false)
const error = ref('')
const controller = new AbortController()

async function loadMore() {
  if (loading.value || controller.signal.aborted) return
  loading.value = true
  error.value = ''
  try {
    const result = await getRunMailToolResults(props.runId, props.toolCallId, {
      offset: items.value.length, limit: 100, signal: controller.signal,
    })
    if (controller.signal.aborted) return
    items.value.push(...(result.items ?? []))
    total.value = result.mail_count ?? items.value.length
    matchedCount.value = result.matched_count ?? null
    queryAction.value = result.query_action ?? ''
    source.value = result.source ?? ''
  } catch (cause) {
    if (!controller.signal.aborted) error.value = cause.response?.data?.detail || cause.message || '无法读取查询结果。'
  } finally {
    loading.value = false
  }
}

function formatTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  }).format(date)
}

onMounted(() => { void loadMore() })
onBeforeUnmount(() => { controller.abort() })
</script>

<template>
  <el-dialog
    :model-value="true"
    title="完整工具查询结果"
    class="mail-tool-result-dialog"
    width="min(800px, calc(100vw - 32px))"
    append-to-body
    @update:model-value="value => { if (!value) emit('close') }"
  >
    <div class="mail-tool-result-content">
      <p class="muted">这是该任务执行时邮件工具返回的完整元数据快照，不含邮件正文；要读正文请使用邮件详情。</p>
      <p v-if="queryAction === 'filter'" class="mail-tool-result-notice">本次任务还对工具结果做了筛选；下方是工具返回的全部邮件，不代表全部都符合本次查询条件。</p>
      <p v-if="total || !loading" class="mail-tool-result-count">工具返回 {{ total }} 封<span v-if="matchedCount !== null"> · 本次匹配 {{ matchedCount }} 封</span><span v-if="source"> · 来源 {{ source }}</span></p>
      <el-alert v-if="error" :title="typeof error === 'string' ? error : error.message || '无法读取查询结果。'" type="error" :closable="false" show-icon />
      <div v-for="(item, index) in items" :key="index" class="mail-tool-result-item">
        <strong>{{ index + 1 }}. {{ item.subject || '（无主题）' }}</strong>
        <span>{{ item.sender_name || item.sender_email }}<template v-if="item.sender_name"> · {{ item.sender_email }}</template> · {{ formatTime(item.received_at) }}</span>
        <p v-if="item.list_preview">{{ item.list_preview }}</p>
        <small v-if="item.attachment_count">附件 {{ item.attachment_count }} 个</small>
      </div>
      <p v-if="!items.length && !loading && !error" class="muted">没有邮件。</p>
      <el-button v-if="items.length < total" :loading="loading" @click="loadMore">继续加载（{{ items.length }} / {{ total }}）</el-button>
      <p v-else-if="loading" class="muted">正在加载…</p>
    </div>
  </el-dialog>
</template>

<style scoped>
:global(.mail-tool-result-dialog) { display: flex; max-height: calc(100dvh - 48px); flex-direction: column; margin: 24px auto !important; overflow: hidden; }
:global(.mail-tool-result-dialog .el-dialog__body) { min-height: 0; overflow-y: auto; }
.mail-tool-result-content { display: grid; gap: 12px; }
.mail-tool-result-content p { margin: 0; }
.mail-tool-result-count { color: #344054; font-weight: 650; }
.mail-tool-result-notice { padding: 9px 11px; border-radius: 7px; background: #fff4e4; color: #8a5b1c; font-size: 12px; }
.mail-tool-result-item { display: grid; gap: 5px; padding: 12px 0; border-top: 1px solid #e7edf5; overflow-wrap: anywhere; }
.mail-tool-result-item span, .mail-tool-result-item small { color: #667085; font-size: 12px; }
.mail-tool-result-item p { color: #344054; font-size: 13px; line-height: 1.5; white-space: pre-wrap; }
</style>
