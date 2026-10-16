<script setup>
import { ref, watch } from 'vue'
import { getKnowledgeSource } from '../api/knowledge'
import { sameSourceSnapshot } from '../utils/knowledge'
const props = defineProps({ source: { type: Object, default: null } })
const emit = defineEmits(['close', 'unavailable'])
const loading = ref(false)
const current = ref(null)
const error = ref('')
watch(() => props.source, async (source, previous, cleanup) => {
  current.value = null
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
    current.value = result
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
        <p class="source-text">{{ current.excerpt }}</p>
        <p class="hash">内容哈希：{{ current.content_hash }}</p>
      </template>
    </div>
  </el-dialog>
</template>

<style scoped>
.source-text { white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.8; }
.hash { font-size:12px;color:#758095;overflow-wrap:anywhere; }
</style>
