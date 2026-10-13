<script setup>
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { listInputRequests, answerInputRequest, cancelInputRequest } from '../api/mail'

const props = defineProps({ runId: { type: String, default: '' } })
const emit = defineEmits(['answered', 'cancelled'])
const requests = ref([])
const answers = ref({})
const clientIds = new Map()
const busy = ref(false)
const error = ref('')
const expiredSeen = new Set()
let alive = true
let timer
let requestSequence = 0

function errorMessage(cause, fallback) {
  const detail = cause.response?.data?.detail
  return (typeof detail === 'object' ? detail?.message : detail) || cause.message || fallback
}

function canAnswer(item) {
  return item.status === 'pending' && item.ready && !item.expired
    && (!item.expires_at || new Date(item.expires_at).getTime() > Date.now())
}

async function load() {
  const requestId = ++requestSequence
  try {
    const result = await listInputRequests(props.runId)
    if (alive && requestId === requestSequence) {
      error.value = ''
      for (const item of result) {
        if (item.status === 'expired' && !expiredSeen.has(item.question_id)) {
          expiredSeen.add(item.question_id)
          emit('cancelled', item.run_id)
        }
      }
      requests.value = result.filter((item) => item.status === 'pending')
    }
  } catch (cause) {
    if (alive && requestId === requestSequence) error.value = errorMessage(cause, '加载补充问题失败。')
  }
}
async function submit(item) {
  const response = answers.value[item.question_id]?.trim()
  if (!response || !canAnswer(item) || busy.value) return
  busy.value = true
  error.value = ''
  try {
    if (clientIds.get(item.question_id)?.response !== response) {
      clientIds.set(item.question_id, { response, id: crypto.randomUUID() })
    }
    const result = await answerInputRequest(item.question_id, {
      expected_version: item.version, client_response_id: clientIds.get(item.question_id).id,
      response,
    })
    await load()
    emit('answered', result.run_id)
  } catch (cause) {
    error.value = errorMessage(cause, '提交补充信息失败。')
    if (cause.response?.status === 409) await load()
  } finally { busy.value = false }
}
async function cancel(item) {
  if (!canAnswer(item) || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const result = await cancelInputRequest(item.question_id)
    await load()
    emit('cancelled', result.run_id)
  } catch (cause) { error.value = errorMessage(cause, '取消起草任务失败。') }
  finally { busy.value = false }
}
onMounted(() => { void load(); timer = window.setInterval(load, 5000) })
watch(() => props.runId, () => { requests.value = []; void load() })
onBeforeUnmount(() => { alive = false; ++requestSequence; window.clearInterval(timer) })
</script>

<template>
  <section v-if="requests.length || error" class="run-input-panel">
    <el-alert v-if="error" :title="String(error)" type="error" :closable="false" />
    <article v-for="item in requests" :key="item.question_id">
      <b>起草任务需要补充信息</b>
      <p>{{ item.question }}</p>
      <p v-if="!item.ready">任务正在保存暂停点，请稍候…</p>
      <p v-if="item.expired || (item.expires_at && new Date(item.expires_at).getTime() <= Date.now())">问题已过期，请重新发起起草任务。</p>
      <el-input v-model="answers[item.question_id]" type="textarea" :rows="3" maxlength="2000" :disabled="busy || !canAnswer(item)" />
      <el-button type="primary" :disabled="!canAnswer(item) || !answers[item.question_id]?.trim()" :loading="busy" @click="submit(item)">提交并继续起草</el-button>
      <el-button :disabled="busy || !canAnswer(item)" @click="cancel(item)">取消任务</el-button>
    </article>
  </section>
</template>

<style scoped>
.run-input-panel { margin: 12px 0; padding: 12px; border: 1px solid #cddced; border-radius: 8px; background: #f6f9fe; }
.run-input-panel article + article { margin-top: 16px; }
.run-input-panel .el-textarea { margin-bottom: 10px; }
</style>
