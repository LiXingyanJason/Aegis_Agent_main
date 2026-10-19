<script setup>
import { computed } from 'vue'

const props = defineProps({ run: { type: Object, default: null } })
// 这是任务状态提示，不伪造assistant消息或将供应商原始错误/敏感内容复制进聊天。
const reason = computed(() => ({
  LLM_UNAVAILABLE: '模型服务调用失败，本次任务未能完成。',
  MODEL_OUTPUT_INVALID: '模型结果未通过校验，本次任务未能完成。',
  EXECUTION_BUDGET_EXCEEDED: '任务执行次数或时间预算已耗尽。',
  EXECUTION_TIMEOUT: '任务执行超时。',
  MEMORY_CONTEXT_CHANGED: '相关记忆或来源已变化，请重新核对。',
}[props.run?.error_code] || '本次任务执行失败，未能完成请求。'))
</script>

<template>
  <div v-if="run?.status === 'failed'" class="message run-failure-message" role="alert">
    <div class="avatar">A</div>
    <div class="run-failure-notice">
      <b>任务未完成</b>
      <p>{{ reason }}</p>
      <p>请查看执行进度核对已执行步骤；排除问题后，可重新提交。此提示不会自动重试或执行工具。</p>
      <small>任务 ID：{{ run.run_id }}</small>
    </div>
  </div>
</template>

<style scoped>
.run-failure-notice { padding: 12px 16px; border: 1px solid #f3c8c8; border-radius: 12px; background: #fff5f5; color: #8c3030; }
.run-failure-notice p { margin: 6px 0; line-height: 1.6; }
.run-failure-notice small { display: block; overflow-wrap: anywhere; }
</style>
