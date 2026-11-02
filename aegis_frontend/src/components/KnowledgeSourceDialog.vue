<script setup>
import { ref, watch } from 'vue'
import { getKnowledgeSource } from '../api/knowledge'
import { samePartSnapshot, sameSourceSnapshot } from '../utils/knowledge'
const props = defineProps({ source: { type: Object, default: null } })
const emit = defineEmits(['close', 'unavailable'])
const loading = ref(false)
const current = ref(null)
const verifiedParts = ref([])
const error = ref('')
watch(() => props.source, async (source, previous, cleanup) => {
  current.value = null
  verifiedParts.value = []
  error.value = ''
  loading.value = false
  if (!source) return
  const controller = new AbortController()
  cleanup(() => controller.abort())
  loading.value = true
  try {
    const result = await getKnowledgeSource(source, controller.signal)
    if (controller.signal.aborted) return
    if (!sameSourceSnapshot(source, result)) {
      error.value = '来源快照已变化，请重新核验或查询。'
      emit('unavailable')
      return
    }
    const parts = []
    for (const part of source.parts || []) {
      const verified = await getKnowledgeSource({
        document_id: source.document_id, version_id: source.version_id,
        generation_id: source.generation_id, chunk_id: part.chunk_id, content_hash: part.content_hash,
      }, controller.signal)
      if (controller.signal.aborted) return
      if (!samePartSnapshot(source, part, verified)) {
        error.value = '扩展片段已变化，请重新核验或查询。'
        emit('unavailable')
        return
      }
      parts.push({ ...verified, origin: part.origin })
    }
    current.value = result
    verifiedParts.value = parts
  } catch (cause) {
    if (controller.signal.aborted) return
    error.value = cause?.response?.status === 404 ? '来源已失效或无权访问，请重新查询。' : '来源暂时无法读取，请关闭后重试。'
    emit('unavailable')
  } finally {
    if (!controller.signal.aborted) loading.value = false
  }
}, { immediate: true })
</script>

<template>
  <el-dialog :model-value="Boolean(source)" title="核对资料来源" width="min(720px, 94vw)" @close="emit('close')">
    <div v-loading="loading" style="min-height:100px">
      <p v-if="error" role="alert">{{ error }}</p>
      <template v-else-if="current">
        <h3>{{ current.title }}</h3>
        <p>{{ current.version_label }} · {{ current.section }} · 字符 {{ current.start_offset }}–{{ current.end_offset }}</p>
        <section class="source-section"><b>主来源 · 字符 {{ current.start_offset }}–{{ current.end_offset }}</b><p class="source-text">{{ current.excerpt }}</p></section>
        <section v-for="part in verifiedParts" :key="part.chunk_id" class="source-section"><b>扩展片段 · 字符 {{ part.start_offset }}–{{ part.end_offset }}</b><p class="source-text">{{ part.excerpt }}</p></section>
        <p v-if="source?.context_incomplete" class="incomplete">扩展内容不完整，不能据此认定已找到所有例外。</p>
        <p class="hash">内容哈希：{{ current.content_hash }}</p>
      </template>
    </div>
  </el-dialog>
</template>

<style scoped>
.source-text { white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.8; }
.hash { font-size:12px;color:#758095;overflow-wrap:anywhere; }
.source-section { margin:12px 0;padding:12px;border:1px solid #dce5f1;border-radius:8px; }
.source-section b { color:#3c5572;font-size:12px; }
.incomplete { color:#93621c; }
</style>
