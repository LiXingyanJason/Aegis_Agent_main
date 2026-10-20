<script setup>
import { computed } from 'vue'
import { cooperationDisplay } from '../utils/cooperationProgress'
const props = defineProps({ run: { type: Object, required: true } })
const progress = computed(() => cooperationDisplay(props.run))
</script>

<template>
  <div v-if="progress" class="cooperation-progress" role="status" aria-live="polite" :data-status="progress.status">
    <span class="badge read">{{ progress.mode_label }}</span>
    <p><span v-if="progress.active" class="cooperation-pulse" aria-hidden="true">● </span>{{ progress.label }}</p>
    <small v-if="progress.step_id">步骤：{{ progress.step_id }}</small>
  </div>
</template>

<style scoped>
.cooperation-progress { margin: 10px 0; padding: 10px; border-left: 3px solid #5b83df; background: #f3f7ff; border-radius: 6px; }
.cooperation-progress p { margin: 8px 0 4px; }
.cooperation-progress small { color: #62718b; }
.cooperation-progress[data-status="failed"], .cooperation-progress[data-status="unavailable"], .cooperation-progress[data-status="blocked"] { border-left-color: #d89025; background: #fff8ed; }
.cooperation-pulse { color: #3277df; }
</style>
