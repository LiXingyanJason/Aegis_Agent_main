<script setup>
import { ref } from 'vue'
import { useAuthStore } from '../store/auth'

const auth = useAuthStore()
const signingIn = ref(false)
const error = ref('')

async function signIn() {
  signingIn.value = true
  error.value = ''
  try {
    await auth.signIn()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法开始登录，请检查 OIDC 配置。'
    signingIn.value = false
  }
}
</script>

<template>
  <main class="auth-page">
    <div class="auth-layout">
      <section class="auth-intro">
        <router-link class="brand auth-brand" to="/login"><span class="mark">⌾</span>Aegis PA</router-link>
        <span class="eyebrow">安全协作工作台</span>
        <h1>让 Agent 在你的授权范围内工作</h1>
        <p>登录后，你可以管理个人对话、邮件草稿、日程确认和长期记忆。外部写入操作仍需逐项确认。</p>
        <div class="auth-points">
          <div><span>✓</span><p><b>统一身份认证</b><br>使用组织的 Keycloak 账号登录，Aegis 不保存你的密码。</p></div>
          <div><span>✓</span><p><b>租户隔离</b><br>系统仅加载你所属工作空间中的数据。</p></div>
          <div><span>✓</span><p><b>令牌不展示</b><br>access token 仅由浏览器 OIDC 客户端管理，不会显示在页面中。</p></div>
        </div>
      </section>
      <section class="auth-card-wrap">
        <article class="auth-card card">
          <span class="badge read">组织单点登录</span>
          <h2>登录 Aegis </h2>
          <p class="muted">点击后将跳转至 Keycloak 的安全登录页面。请使用你的组织账号完成验证。</p>
          <el-alert v-if="error" class="login-error" :title="error" type="error" :closable="false" show-icon />
          <el-button class="btn auth-login" type="primary" :loading="signingIn" @click="signIn">
            使用 Keycloak 登录
          </el-button>
          <div class="auth-note"><b>安全说明</b><br>使用 OIDC 授权码模式 + PKCE；不会请求、展示或持久化保存 token。</div>
          <p class="auth-foot">需要加入工作空间？请联系组织管理员。</p>
        </article>
      </section>
    </div>
  </main>
</template>
