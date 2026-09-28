<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../store/auth'

const router = useRouter()
const auth = useAuthStore()
const error = ref('')

onMounted(async () => {
  try {
    const authenticated = await auth.initialize()
    if (!authenticated) throw new Error('Keycloak 未返回有效登录会话。')
    await router.replace({ name: 'tasks' })
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '登录回调处理失败。'
  }
})
</script>

<template>
  <main class="callback-page">
    <section class="callback-shell">
      <router-link class="brand auth-brand" to="/login"><span class="mark">⌾</span>Aegis PA</router-link>
      <article class="callback-card card">
        <div v-if="!error" class="spinner" aria-hidden="true"></div>
        <span class="eyebrow">OIDC 登录回调</span>
        <h1>{{ error ? '无法完成登录' : '正在完成安全登录' }}</h1>
        <p class="muted">{{ error || '浏览器正在校验 Keycloak 返回结果并建立当前会话。' }}</p>
        <div class="auth-flow">
          <div class="done"><span>1</span>已从 Keycloak 返回</div>
          <div :class="{ active: !error }"><span>2</span>获取并管理 access token</div>
          <div><span>3</span>加载 Aegis 工作空间</div>
        </div>
        <div class="auth-result" :class="error ? 'error' : 'neutral'">
          <b>{{ error ? '登录验证失败' : '正在验证身份' }}</b>
          <span>{{ error ? '请返回后重新登录，或联系组织管理员。' : '不会展示或写入 access token。' }}</span>
        </div>
        <el-button v-if="error" class="btn auth-login" type="primary" @click="router.replace({ name: 'login' })">返回登录页 <span>→</span></el-button>
        <p class="auth-foot">若长时间未完成，请返回后重新登录。</p>
      </article>
    </section>
  </main>
</template>
