<script setup>
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { listInputRequests, answerInputRequest, cancelInputRequest } from '../api/mail'
import { getRunDetail } from '../api/run'
import { inputKindHint, inputKindTitle } from '../utils/cooperation'
import { buildInputAnswerPayload, inputAnswerKey, inputRequestBelongsToScope, inputRequestType } from '../utils/inputRequest'

const props = defineProps({
  runId: { type: String, default: '' },
  runIds: { type: Array, default: null },
  scope: { type: String, default: 'all' },
  conversationId: { type: String, default: '' },
  inline: { type: Boolean, default: false },
})
const emit = defineEmits(['answered', 'cancelled'])
const requests = ref([])
const answers = ref({})
const selectedOptions = ref({})
const clientIds = new Map()
const questionVersions = new Map()
const runConversationIds = new Map()
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

function canSubmit(item) {
  return !busy.value && canAnswer(item) && Boolean(buildInputAnswerPayload(item, {
    response: answers.value[item.question_id],
    selectedOptionId: selectedOptions.value[item.question_id],
  }, 'preview'))
}

async function load() {
  const requestId = ++requestSequence
  try {
    const result = await listInputRequests(props.runId)
    const candidates = result.filter((item) => item.status === 'pending' || item.status === 'expired')
    const unknownRunIds = (props.scope === 'all' ? [] : [...new Set(candidates.map((item) => item.run_id))])
      .filter((runId) => runId && !runConversationIds.has(runId)
        && !(props.scope === 'conversation' && props.runIds?.includes(runId)))
    await Promise.all(unknownRunIds.map(async (runId) => {
      try {
        const run = await getRunDetail(runId)
        if (Object.hasOwn(run, 'conversation_id')) runConversationIds.set(runId, run.conversation_id)
      } catch {
        // 归属未核验时不展示可提交的补问，等待下一次刷新重试。
      }
    }))
    if (alive && requestId === requestSequence) {
      const scoped = candidates.filter((item) => inputRequestBelongsToScope(item, props, runConversationIds))
      error.value = unknownRunIds.some((runId) => !runConversationIds.has(runId))
        ? '部分补充问题的所属任务暂无法核验，将自动重试。' : ''
      for (const item of scoped) {
        const questionKey = `${item.version}:${inputRequestType(item)}`
        if (questionVersions.has(item.question_id) && questionVersions.get(item.question_id) !== questionKey) {
          delete answers.value[item.question_id]
          delete selectedOptions.value[item.question_id]
          clientIds.delete(item.question_id)
        }
        questionVersions.set(item.question_id, questionKey)
        if (inputRequestType(item) === 'single_choice' && selectedOptions.value[item.question_id]
          && !item.options?.some((option) => option.id === selectedOptions.value[item.question_id])) {
          delete selectedOptions.value[item.question_id]
          clientIds.delete(item.question_id)
        }
        if (item.status === 'expired' && !expiredSeen.has(item.question_id)) {
          expiredSeen.add(item.question_id)
          emit('cancelled', item.run_id)
        }
      }
      requests.value = scoped.filter((item) => item.status === 'pending')
    }
  } catch (cause) {
    if (alive && requestId === requestSequence) error.value = errorMessage(cause, '加载补充问题失败。')
  }
}
async function submit(item) {
  if (!canAnswer(item) || busy.value) return
  const draft = buildInputAnswerPayload(item, {
    response: answers.value[item.question_id],
    selectedOptionId: selectedOptions.value[item.question_id],
  }, 'preview')
  if (!draft) return
  busy.value = true
  error.value = ''
  try {
    const answerKey = inputAnswerKey(item, draft)
    if (clientIds.get(item.question_id)?.key !== answerKey) {
      clientIds.set(item.question_id, { key: answerKey, id: crypto.randomUUID() })
    }
    draft.client_response_id = clientIds.get(item.question_id).id
    const result = await answerInputRequest(item.question_id, draft)
    await load()
    emit('answered', result.run_id)
  } catch (cause) {
    if (cause.response?.status === 409) {
      await load()
      error.value = inputRequestType(item) === 'single_choice'
        ? '所选邮件或问题状态已变化，请核对当前选项；若选项失效，请重新发起任务。'
        : errorMessage(cause, '问题状态已变化，请核对后重试。')
    } else {
      error.value = errorMessage(cause, '提交补充信息失败。')
    }
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
  } catch (cause) { error.value = errorMessage(cause, '取消任务失败。') }
  finally { busy.value = false }
}
onMounted(() => { void load(); timer = window.setInterval(load, 5000) })
watch(() => props.runId, () => { requests.value = []; void load() })
watch(() => props.runIds, () => { requests.value = []; void load() })
watch(() => props.conversationId, () => { requests.value = []; void load() })
onBeforeUnmount(() => { alive = false; ++requestSequence; window.clearInterval(timer) })
</script>

<template>
  <section v-if="requests.length || error" :id="inline ? `run-input-${runId}` : undefined" :class="inline ? 'message assistant run-input-message' : 'run-input-panel'">
    <div v-if="inline" class="avatar" aria-hidden="true">A</div>
    <div :class="{ 'run-input-panel': inline }">
      <el-alert v-if="error" :title="String(error)" type="error" :closable="false" />
      <article v-for="item in requests" :key="item.question_id">
        <b>{{ inputKindTitle(item.cooperation?.input_kind) }}</b>
        <p>{{ item.question }}</p>
        <p v-if="inputKindHint(item.cooperation?.input_kind, inputRequestType(item))" class="input-hint">{{ inputKindHint(item.cooperation?.input_kind, inputRequestType(item)) }}</p>
        <p v-if="!item.ready">任务正在保存暂停点，请稍候…</p>
        <p v-if="item.expired || (item.expires_at && new Date(item.expires_at).getTime() <= Date.now())">问题已过期，请重新发起任务。</p>
        <el-radio-group v-if="inputRequestType(item) === 'single_choice'" v-model="selectedOptions[item.question_id]" class="input-choice-list" :disabled="busy || !canAnswer(item)" aria-label="选择邮件">
          <el-radio v-for="option in item.options ?? []" :key="option.id" :value="option.id" class="input-choice-option">{{ option.label }}</el-radio>
        </el-radio-group>
        <el-alert v-if="inputRequestType(item) === 'single_choice' && !item.options?.length" title="此问题暂无可用选项，请重新发起任务。" type="warning" :closable="false" />
        <el-input v-if="inputRequestType(item) === 'text'" v-model="answers[item.question_id]" type="textarea" :rows="3" maxlength="2000" :disabled="busy || !canAnswer(item)" />
        <el-alert v-if="inputRequestType(item) === 'unsupported'" title="暂不支持这种补问类型，请刷新页面或联系管理员。" type="warning" :closable="false" />
        <el-button type="primary" :disabled="!canSubmit(item)" :loading="busy" @click="submit(item)">提交并继续任务</el-button>
        <el-button :disabled="busy || !canAnswer(item)" @click="cancel(item)">取消任务</el-button>
      </article>
    </div>
  </section>
</template>

<style scoped>
.run-input-panel { margin: 12px 0; padding: 12px; border: 1px solid #cddced; border-radius: 8px; background: #f6f9fe; }
.run-input-panel article + article { margin-top: 16px; }
.run-input-panel .el-textarea { margin-bottom: 10px; }
.input-hint { color: #667085; font-size: 12px; }
.input-choice-list { display: grid; gap: 7px; width: 100%; margin: 0 0 12px; }
.input-choice-option { box-sizing: border-box; width: 100%; height: auto; min-height: 36px; margin: 0 !important; padding: 8px 10px; border: 1px solid #d7e1ef; border-radius: 7px; background: #fff; white-space: normal; }
.input-choice-option.is-checked { border-color: #409eff; background: #f0f7ff; }
.input-choice-option :deep(.el-radio__label) { white-space: normal; overflow-wrap: anywhere; line-height: 1.45; }
.run-input-message { width: 100%; max-width: 100%; align-self: flex-start; }
.run-input-message > .run-input-panel { width: min(540px, calc(100% - 41px)); max-height: none; margin: 0; overflow: visible; }
.run-input-message { scroll-margin-top: 18px; }
</style>
