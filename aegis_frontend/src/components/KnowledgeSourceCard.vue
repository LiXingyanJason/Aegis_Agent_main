<script setup>
import { computed } from 'vue'
const props = defineProps({ artifact: { type: Object, required: true }, error: { type: String, default: '' } })
const sources = computed(() => props.error || ['unavailable', 'loading'].includes(props.artifact.payload?.availability)
  ? [] : props.artifact.payload?.sources ?? [])
defineEmits(['open-source', 'refresh'])
</script>

<template>
  <section class="knowledge-source-card" aria-label="知识来源">
    <header><b>资料来源</b><button type="button" @click="$emit('refresh')">重新核验</button></header>
    <p v-if="artifact.payload?.has_conflict" class="warning">资料存在冲突，请核对不同依据；不能据此确认唯一结论。</p>
    <p v-if="error" role="alert">来源读取失败，请重试；不展示旧缓存。{{ error }}</p>
    <p v-else-if="artifact.payload?.availability === 'loading'">正在重新核验来源…</p>
    <p v-else-if="artifact.payload?.availability === 'partial'" class="warning">部分依据已失效，保留原编号；剩余来源不自动成为唯一结论。</p>
    <p v-else-if="artifact.payload?.availability === 'unavailable' || !artifact.payload?.sources?.length">来源已失效或无权读取，请重新查询。</p>
    <article v-for="source in sources" :key="source.chunk_id">
      <button type="button" @click="$emit('open-source', source)">[{{ source.source_no }}] {{ source.title }}</button>
      <p>{{ source.version_label }} · {{ source.section }} · 字符 {{ source.start_offset }}–{{ source.end_offset }}</p>
      <div class="source-segment"><span>主来源 · 原文范围 {{ source.start_offset }}–{{ source.end_offset }}</span><blockquote>{{ source.excerpt }}</blockquote></div>
      <div v-for="part in source.parts || []" :key="part.chunk_id" class="source-segment extension">
        <span>扩展片段 · 原文范围 {{ part.start_offset }}–{{ part.end_offset }} · {{ { same_clause: '同条款', adjacent: '相邻内容', related: '相关内容' }[part.origin] || '相关原文' }}</span>
        <blockquote>{{ part.excerpt }}</blockquote>
      </div>
      <p v-if="source.context_incomplete" class="warning">扩展内容不完整，不能据此认定已找到所有例外。</p>
    </article>
  </section>
</template>

<style scoped>
.knowledge-source-card { background:#f7faff;border:1px solid #ccdcf7;border-radius:14px;padding:18px;width:min(100%,720px);overflow-wrap:anywhere; }
header { display:flex;justify-content:space-between;align-items:center;gap:12px; }
button { color:#245ca8;background:transparent;border:0;cursor:pointer;text-align:left;font:inherit;padding:4px 0; }
article { border-top:1px solid #dce5f1;margin-top:12px;padding-top:10px; }
p { font-size:13px;color:#637086; }.warning { color:#93621c; }
blockquote { white-space:pre-wrap;margin:8px 0;color:#26394f; }
.source-segment { margin-top:9px;padding:9px 11px;border:1px solid #dce5f1;border-radius:8px;background:#fff; }
.source-segment.extension { background:#f9fbff; }
.source-segment span { color:#526782;font-size:12px;font-weight:700; }
</style>
