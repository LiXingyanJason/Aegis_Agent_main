<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../store/auth'
import { useConversationStore } from '../store/conversation'

const auth = useAuthStore()
const conversations = useConversationStore()
const router = useRouter()
const draftMessage = ref('')
const conversationTitle = ref('')
const pendingClientMessageId = ref(null)
const error = ref('')

const activeConversation = computed(() => conversations.current)

function formatTime(value) {
  if (!value) return '暂无消息'
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

async function loadHistory() {
  error.value = ''
  try {
    await conversations.loadHistory()
  } catch (cause) {
    error.value = cause.response?.data?.detail || cause.message || '加载历史会话失败。'
  }
}

async function restoreConversation(conversationId) {
  error.value = ''
  try {
    await conversations.loadConversation(conversationId)
    draftMessage.value = ''
    pendingClientMessageId.value = null
  } catch (cause) {
    error.value = cause.response?.data?.detail || cause.message || '恢复会话失败。'
  }
}

async function startNewConversation() {
  error.value = ''
  try {
    await conversations.start(conversationTitle.value.trim())
    conversationTitle.value = ''
    draftMessage.value = ''
    pendingClientMessageId.value = null
  } catch (cause) {
    error.value = cause.response?.data?.detail || cause.message || '创建会话失败。'
  }
}

async function sendMessage() {
  const content = draftMessage.value.trim()
  if (!content || !activeConversation.value) return
  error.value = ''
  try {
    pendingClientMessageId.value ??= crypto.randomUUID()
    await conversations.send(content, pendingClientMessageId.value)
    draftMessage.value = ''
    pendingClientMessageId.value = null
  } catch (cause) {
    error.value = cause.response?.data?.detail || cause.message || '发送消息失败。'
  }
}

async function signOut() {
  await auth.signOut()
  await router.replace({ name: 'login' })
}

onMounted(loadHistory)
</script>

<template>
  <main class="workspace">
    <aside class="sidebar">
      <div class="brand"><span class="mark">⌾</span>Aegis PA</div>
      <p class="nav-label app-nav-label">工作空间</p>
      <a class="nav active" href="#task-console">◌ 任务对话</a>
      <span class="nav disabled">✓ 操作确认（待接入）</span>
      <span class="nav disabled">◇ 长期记忆（待接入）</span>
      <span class="nav disabled">◫ 连接与审计（待接入）</span>

      <div class="identity">
        <b>{{ auth.profile?.name }}</b>
        <span>{{ auth.profile?.email || '已通过 Keycloak 登录' }}</span>
        <el-button text type="primary" @click="signOut">退出登录</el-button>
      </div>
    </aside>

    <section id="task-console" class="content">
      <header class="page-header">
        <div>
          <h1>{{ activeConversation?.title || '任务对话' }}</h1>
          <p class="muted">{{ activeConversation ? `会话 ID：${activeConversation.conversation_id}` : '从左侧选择一条历史对话，或创建新的任务。' }}</p>
        </div>
        <span class="badge success">● 身份已验证</span>
      </header>

      <div class="task-workspace">
        <aside class="conversation-history" aria-label="历史对话">
          <div class="new-title-editor">
            <label for="conversation-title">对话标题</label>
            <el-input id="conversation-title" v-model="conversationTitle" maxlength="200" placeholder="例如：明日项目同步" />
            <el-button class="new-chat-button" type="primary" :loading="conversations.creating" @click="startNewConversation">
              <span>＋</span> 新建对话
            </el-button>
            <p>可选，不填写时使用“新对话”。</p>
          </div>
          <p class="conversation-history-label">历史对话</p>
          <div v-loading="conversations.loadingHistory" class="history-scroll">
            <button
              v-for="conversation in conversations.history"
              :key="conversation.conversation_id"
              class="history-item"
              :class="{ active: activeConversation?.conversation_id === conversation.conversation_id }"
              type="button"
              @click="restoreConversation(conversation.conversation_id)"
            >
              <span class="history-title">{{ conversation.title }}</span>
              <span class="history-time">{{ formatTime(conversation.last_message_at || conversation.created_at) }}</span>
            </button>
            <p v-if="!conversations.loadingHistory && conversations.history.length === 0" class="history-empty">还没有历史对话。</p>
          </div>
        </aside>

        <div class="console-grid">
          <article v-loading="conversations.loadingDetail" class="card conversation-card">
            <div class="card-head">
              <div>
                <h2>{{ activeConversation ? '已恢复的历史对话' : '开始新的任务' }}</h2>
                <p class="muted">{{ activeConversation ? '已恢复该会话的完整可见消息。' : '选择一条历史会话，或在左侧创建新会话。' }}</p>
              </div>
            </div>

            <el-alert v-if="error" class="conversation-error" :title="error" type="error" :closable="false" show-icon />
            <div v-if="activeConversation" class="messages">
              <div v-for="item in conversations.messages" :key="item.message_id" class="message" :class="item.role">
                <div class="avatar">{{ item.role === 'user' ? '你' : 'A' }}</div>
                <div>
                  <div class="message-bubble">{{ item.content }}</div>
                  <div class="message-meta">{{ item.role === 'user' ? '你' : 'Aegis' }} · {{ formatTime(item.created_at) }}</div>
                </div>
              </div>
              <div v-if="conversations.messages.length === 0" class="empty-messages">此会话暂无可见消息。</div>
              <div v-if="conversations.latestSubmission?.status === 'queued'" class="queued-notice">
                <b>任务已提交，等待处理</b>
                <span>任务 ID：{{ conversations.latestSubmission.run_id }}</span>
              </div>
            </div>
            <div v-else class="empty-state">
              <div class="empty-mark">⌁</div>
              <h3>选择历史对话或开始新任务</h3>
              <p>进入页面后已加载当前用户的历史会话；在左侧填写标题并点击“新建对话”即可创建新会话。</p>
            </div>

            <form class="new-conversation" @submit.prevent="sendMessage">
              <el-input v-model="draftMessage" maxlength="8000" placeholder="输入任务，例如：帮我整理今天的邮件" aria-label="输入任务" :disabled="!activeConversation" />
              <el-button native-type="submit" type="primary" :loading="conversations.sending" :disabled="!activeConversation">发送</el-button>
            </form>
          </article>

          <aside>
            <article class="card side-card">
              <div class="card-head"><h2>安全状态</h2><span class="badge read">OIDC + PKCE</span></div>
              <div class="card-body status-list">
                <p><b>访问令牌</b><span>请求前自动刷新并以 Bearer 形式发送。</span></p>
                <p><b>租户身份</b><span>由后端从已验证 token 映射，前端不提交。</span></p>
                <p><b>运行记录</b><span>{{ activeConversation ? `已恢复 ${conversations.runs.length} 条关联运行记录。` : '选择会话后显示关联运行状态。' }}</span></p>
              </div>
            </article>
          </aside>
        </div>
      </div>
    </section>
  </main>
</template>
