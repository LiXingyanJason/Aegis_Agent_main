<script setup>
import { ref, watch, onBeforeUnmount } from 'vue'
import { getRunContextSummary } from '../api/run'
import { memoryUsageState, memoryPurposeLabel, memoryAttemptStatus, memorySelectionReason, memorySourceKind, tokenCount, memoryExclusionLabels } from '../utils/memoryUsage'

const props = defineProps({ runId: { type: String, default: null } })
const emit = defineEmits(['close'])
const summary = ref(null)
const loading = ref(false)
const error = ref('')
let version = 0
let controller = null

async function load() {
  const current = ++version
  controller?.abort()
  summary.value = null
  error.value = ''
  loading.value = Boolean(props.runId)
  if (!props.runId) return
  controller = new AbortController()
  try {
    const result = await getRunContextSummary(props.runId, { signal: controller.signal })
    if (current === version) summary.value = result
  } catch {
    if (current === version) error.value = '无法读取记忆使用情况，请重试；未展示旧缓存。'
  } finally { if (current === version) loading.value = false }
}
watch(() => props.runId, load, { immediate: true })
onBeforeUnmount(() => { ++version; controller?.abort(); summary.value = null })
</script>

<template>
  <el-dialog :model-value="Boolean(runId)" title="记忆使用记录" width="min(760px, calc(100vw - 32px))" class="run-memory-dialog" @update:model-value="!$event && emit('close')">
    <div class="memory-dialog-intro">
      <strong>只读追溯</strong>
      <p>展示本次任务的记忆引用与调用记录，不展示记忆正文或完整提示词。历史使用不代表现在仍有效；Token 是整次模型调用用量，并非单条记忆费用。</p>
    </div>
    <div class="memory-dialog-toolbar">
      <span>当前状态以最近一次核验结果为准</span>
      <el-button size="small" type="primary" plain :loading="loading" @click="load">刷新核验结果</el-button>
    </div>
    <div v-if="loading" class="memory-dialog-empty">正在读取当前状态…</div>
    <el-alert v-else-if="error" :title="error" type="error" :closable="false" show-icon />
    <div v-else-if="summary" class="memory-attempts">
      <el-alert v-if="summary.truncated" title="记录较多，仅展示当前有界范围，不代表全部历史调用。" type="info" :closable="false" />
      <div v-if="!summary.attempts.length && !summary.legacy_memories.length" class="memory-dialog-empty">暂无可展示的模型调用或记忆使用记录。</div>
      <section v-for="attempt in summary.attempts" :key="attempt.attempt_id" class="memory-attempt-card">
        <div class="memory-section-heading"><h3>{{ memoryPurposeLabel(attempt.purpose) }}</h3><span class="memory-state">{{ memoryAttemptStatus(attempt.status) }}</span></div>
        <div class="memory-attempt-meta">
          <span>调用时间：{{ attempt.started_at ? new Date(attempt.started_at).toLocaleString('zh-CN') : '未知' }}</span>
          <span>记忆数量：{{ attempt.memory_count ?? '未知' }}</span>
          <span>输入 / 输出 / 总 Token：{{ tokenCount(attempt.prompt_tokens) }} / {{ tokenCount(attempt.completion_tokens) }} / {{ tokenCount(attempt.total_tokens) }}</span>
        </div>
        <p>选择结果：{{ memorySelectionReason(attempt.selection_reason) }} · 策略：{{ attempt.ranking_policy_version || attempt.memory_policy_version || '未知' }}</p>
        <p v-if="attempt.recall_truncated">候选达到召回上限，排除数量仅统计已检查的候选。</p>
        <p v-for="reason in memoryExclusionLabels(attempt.excluded_counts)" :key="reason.key">{{ reason.label }}：{{ reason.count }} 条</p>
        <el-alert v-if="attempt.needs_review" title="存在未注入的冲突或含义不明确的记忆，请到长期记忆页面核对。" type="warning" :closable="false" />
        <ul v-if="attempt.memories.length" class="memory-record-list">
          <li v-for="(memory, index) in attempt.memories" :key="memory.memory_id + '-' + index">
            <div class="memory-record-heading"><strong>{{ memory.included_in_prompt ? '当时已注入' : '未确认注入' }}</strong><span class="memory-state" :class="{ 'is-deleted': memory.current_status === 'deleted' }">当前：{{ memoryUsageState(memory.current_status) }}</span></div>
            <span>{{ memory.memory_type === 'entity' ? '结构化设置' : memory.memory_type === 'long_term_summary' ? '长期摘要' : '类型未知' }} · 版本 {{ memory.memory_version ?? '未知' }} · {{ memorySelectionReason(memory.selection_reason) }} · 来源：{{ memorySourceKind(memory.source_kind) }}</span>
            <code>记忆 ID：{{ memory.memory_id }}</code>
          </li>
        </ul>
        <p v-else class="memory-muted">未记录实际注入的记忆，或当前调用尚未完成审计。</p>
      </section>
      <section v-if="summary.legacy_memories.length" class="memory-attempt-card">
        <div class="memory-section-heading"><h3>旧版任务级记录</h3><span class="memory-count">{{ summary.legacy_memories.length }} 条</span></div>
        <p class="memory-muted">这些历史引用缺少完整的调用或版本关联；“当前状态”不改变当时的使用事实。</p>
        <ul class="memory-record-list">
          <li v-for="(memory,index) in summary.legacy_memories" :key="memory.memory_id + '-' + index">
            <div class="memory-record-heading"><strong>历史记忆引用</strong><span class="memory-state" :class="{ 'is-deleted': memory.current_status === 'deleted' }">当前：{{ memoryUsageState(memory.current_status) }}</span></div>
            <code>记忆 ID：{{ memory.memory_id }}</code>
          </li>
        </ul>
      </section>
    </div>
    <template #footer>
      <div class="memory-dialog-footer">
        <router-link :to="{ name: 'memories' }" class="memory-manage-link" @click="emit('close')">管理长期记忆</router-link>
        <el-button @click="emit('close')">关闭</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
:global(.run-memory-dialog) { display: flex; max-height: calc(100dvh - 48px); flex-direction: column; margin: 24px auto !important; overflow: hidden; }
:global(.run-memory-dialog .el-dialog__header), :global(.run-memory-dialog .el-dialog__footer) { flex: none; }
:global(.run-memory-dialog .el-dialog__body) { min-height: 0; overflow-y: auto; }
.memory-dialog-intro { padding: 13px 15px; border-radius: 8px; background: #f6f9fd; }
.memory-dialog-intro strong { color: #1e5da4; font-size: 12px; }
.memory-dialog-intro p { margin: 5px 0 0; color: #526176; font-size: 13px; line-height: 1.6; }
.memory-dialog-toolbar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; padding: 13px 0; color: #667085; font-size: 12px; }
.memory-dialog-empty { padding: 35px 16px; color: #667085; text-align: center; }
.memory-attempts { display: grid; gap: 12px; }
.memory-attempt-card { min-width: 0; padding: 15px; border: 1px solid #dfe5ee; border-radius: 9px; background: #fff; }
.memory-section-heading, .memory-record-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.memory-section-heading { margin-bottom: 9px; }
.memory-section-heading h3 { margin: 0; color: #1f354d; font-size: 14px; }
.memory-count, .memory-state { padding: 3px 8px; border-radius: 999px; background: #edf4ff; color: #245f9e; font-size: 11px; font-weight: 700; }
.memory-state.is-deleted { background: #f1f4f8; color: #526176; }
.memory-attempt-meta { display: flex; flex-wrap: wrap; gap: 5px 16px; color: #667085; font-size: 12px; }
.memory-attempt-card p { margin: 8px 0 0; color: #526176; font-size: 12px; line-height: 1.6; }
.memory-attempt-card .memory-muted { color: #667085; }
.memory-record-list { display: grid; gap: 8px; margin: 12px 0 0; padding: 0; list-style: none; }
.memory-record-list li { display: grid; gap: 5px; min-width: 0; padding: 10px 12px; border: 1px solid #e4eaf3; border-radius: 7px; background: #f9fbfe; color: #526176; font-size: 12px; line-height: 1.5; }
.memory-record-heading strong { color: #344054; font-size: 12px; }
.memory-record-list code { color: #667085; font-size: 11px; overflow-wrap: anywhere; }
.memory-dialog-footer { display: flex; align-items: center; justify-content: flex-end; gap: 12px; }
.memory-manage-link { display: inline-flex; align-items: center; min-height: 32px; padding: 6px 11px; border: 1px solid #b8d4f5; border-radius: 5px; color: #266dcc; font-size: 12px; font-weight: 700; text-decoration: none; }
.memory-manage-link:hover, .memory-manage-link:focus-visible { background: #edf5ff; }
</style>
