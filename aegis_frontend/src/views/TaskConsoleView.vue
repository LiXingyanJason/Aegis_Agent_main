<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../store/auth'
import { useConversationStore } from '../store/conversation'

const auth = useAuthStore()
const conversations = useConversationStore()
const router = useRouter()
const message = ref('')
const failure = ref('')
const conversationId = computed(() => conversations.current?.conversation_id ?? '尚未创建')

async function createOnEnter() {
  failure.value = ''
  try {
    await conversations.start()
  } catch (cause) {
    failure.value = cause.response?.data?.detail || cause.message || '创建会话失败。'
  }
}

async function send() {
  const content = message.value.trim()
  if (!content) return

  failure.value = ''
  try {
    await conversations.send(content)
    message.value = ''
  } catch (cause) {
    failure.value = cause.response?.data?.detail || cause.message || '发送消息失败。'
  }
}

onMounted(createOnEnter)

async function signOut() {
  await auth.signOut()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <main class="workspace">
    <aside class="sidebar">
      <div class="brand"><span class="mark">⌾</span>Aegis PA</div>
      <p class="nav-label">工作空间</p>
      <a class="nav active" href="#task-console">◌ 任务对话</a>
      <span class="nav disabled">✓ 操作确认（待接入）</span>
      <span class="nav disabled">◇ 长期记忆（待接入）</span>
      <div class="identity">
        <b>{{ auth.profile?.name }}</b>
        <span>{{ auth.profile?.email || '已通过 Keycloak 登录' }}</span>
        <el-button text type="primary" @click="signOut">退出登录</el-button>
      </div>
    </aside>
    <section id="task-console" class="content">
      <header class="page-header">
        <div>
          <h1>任务对话</h1>
          <p class="muted">会话 ID：{{ conversationId }}</p>
        </div>
        <span class="badge success">● 身份已验证</span>
      </header>
      <div class="console-grid">
        <article class="card conversation-card">
          <div class="card-head">
            <div><h2>任务对话</h2><p class="muted">进入页面时已自动建立当前任务会话。</p></div>
          </div>
          <div class="empty-state">
            <div class="empty-mark">⌁</div>
            <h3>{{ conversations.current ? conversations.current.title : '正在建立安全会话' }}</h3>
            <p>{{ conversations.current ? '会话已准备就绪。发送任务后，系统会使用当前登录身份处理请求。' : '正在以当前 access token 创建会话；租户与用户身份只由后端从令牌解析。' }}</p>
          </div>
          <el-alert v-if="failure" :title="failure" type="error" :closable="false" show-icon />
          <form class="new-conversation" @submit.prevent="send">
            <el-input v-model="message" maxlength="8000" placeholder="输入任务，例如：帮我整理今天的邮件" aria-label="输入任务" :disabled="!conversations.current" />
            <el-button native-type="submit" type="primary" :loading="conversations.sending" :disabled="!conversations.current">发送</el-button>
          </form>
        </article>
        <aside>
          <article class="card side-card">
            <div class="card-head"><h2>安全状态</h2><span class="badge read">OIDC + PKCE</span></div>
            <div class="card-body status-list">
              <p><b>访问令牌</b><span>请求前自动刷新并以 Bearer 形式发送。</span></p>
              <p><b>租户身份</b><span>由后端从已验证 token 映射，前端不提交。</span></p>
              <p><b>401 响应</b><span>自动返回登录页，避免继续使用失效令牌。</span></p>
            </div>
          </article>
        </aside>
      </div>
    </section>
  </main>
</template>
