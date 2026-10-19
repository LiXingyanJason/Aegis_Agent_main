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
  <el-dialog :model-value="Boolean(runId)" title="本次任务使用的记忆" width="min(800px, 95vw)" @update:model-value="!$event && emit('close')">
    <p>仅展示记忆引用与调用信息，不展示记忆正文或完整提示词。历史使用不代表现在仍有效；Token 为整次调用用量，不是单条记忆费用。</p>
    <el-button size="small" :loading="loading" @click="load">重新核验</el-button>
    <p v-if="loading">正在读取当前状态…</p>
    <el-alert v-else-if="error" :title="error" type="error" :closable="false" />
    <div v-else-if="summary" class="memory-attempts">
      <p v-if="summary.truncated">记录较多，仅展示当前有界范围，不代表全部历史调用。</p>
      <p v-if="!summary.attempts.length && !summary.legacy_memories.length">尚无可展示的模型调用审计。</p>
      <section v-for="attempt in summary.attempts" :key="attempt.attempt_id">
        <h4>{{ memoryPurposeLabel(attempt.purpose) }} · {{ memoryAttemptStatus(attempt.status) }}</h4>
        <p>调用时间：{{ attempt.started_at ? new Date(attempt.started_at).toLocaleString('zh-CN') : '未知' }} · 记忆数量：{{ attempt.memory_count ?? '未知' }}</p>
        <p>选择结果：{{ memorySelectionReason(attempt.selection_reason) }} · 选择策略：{{ attempt.ranking_policy_version || attempt.memory_policy_version || '未知' }}</p>
        <p>输入 / 输出 / 总 Token：{{ tokenCount(attempt.prompt_tokens) }} / {{ tokenCount(attempt.completion_tokens) }} / {{ tokenCount(attempt.total_tokens) }}</p>
        <p v-if="attempt.recall_truncated">候选达到召回上限，以下排除数量仅统计已检查的候选，不代表全部记忆。</p>
        <p v-for="reason in memoryExclusionLabels(attempt.excluded_counts)" :key="reason.key">{{ reason.label }}：{{ reason.count }} 条</p>
        <el-alert v-if="attempt.needs_review" title="存在未注入的冲突或含义不明确的记忆，请在长期记忆页面核对。" type="warning" :closable="false" />
        <ul>
          <li v-for="(memory, index) in attempt.memories" :key="memory.memory_id + '-' + index">
            <strong>{{ memory.included_in_prompt ? '当时已注入' : '未确认注入' }}</strong> ·
            <code>{{ memory.memory_id }}</code> · v{{ memory.memory_version ?? '未知' }} ·
            {{ memory.memory_type === 'entity' ? '结构化设置' : memory.memory_type === 'long_term_summary' ? '长期摘要' : '类型未知' }} ·
            当前：{{ memoryUsageState(memory.current_status) }}<br>
            选择原因：{{ memorySelectionReason(memory.selection_reason) }} · 来源：{{ memorySourceKind(memory.source_kind) }}
          </li>
        </ul>
        <p v-if="!attempt.memories.length">未记录实际注入的记忆（或当前调用尚未完成审计）。</p>
      </section>
      <section v-if="summary.legacy_memories.length"><h4>旧版任务级记录（调用/版本信息可能未知）</h4>
        <p v-for="(memory,index) in summary.legacy_memories" :key="memory.memory_id + '-' + index"><code>{{ memory.memory_id }}</code> · 当前：{{ memoryUsageState(memory.current_status) }}</p>
      </section>
    </div>
    <template #footer><router-link :to="{ name: 'memories' }" @click="emit('close')">管理长期记忆</router-link><el-button @click="emit('close')">关闭</el-button></template>
  </el-dialog>
</template>

<style scoped>
.memory-attempts { max-height: 60vh; overflow: auto; }
section { border-top: 1px solid #e4e7ed; padding: 12px 0; }
code { overflow-wrap: anywhere; }
p, li { font-size: 13px; line-height: 1.7; }
</style>
