<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../store/auth'

const auth = useAuthStore()
const router = useRouter()
const query = ref('')
const editorVisible = ref(false)
const deleteVisible = ref(false)
const editing = ref(null)
const memoryForm = ref({ content: '', isSensitive: false })

const memories = ref([
  {
    id: 'memory-1',
    content: '周一上午不安排会议',
    note: '对日程候选时间建议生效。',
    source: 'user_input',
    updatedAt: '今天 09:15',
    isSensitive: false,
  },
  {
    id: 'memory-2',
    content: '默认时区为 Asia/Shanghai',
    note: '用于显示会议时间和截止时间。',
    source: 'user_confirmed',
    updatedAt: '2026/9/20',
    isSensitive: false,
  },
  {
    id: 'memory-3',
    content: '涉及家庭地址的信息仅在用户明确提出时使用',
    note: '限制敏感偏好的上下文注入范围。',
    source: 'user_input',
    updatedAt: '2026/9/12',
    isSensitive: true,
  },
])

const filteredMemories = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  if (!keyword) return memories.value
  return memories.value.filter((item) => (
    item.content.toLowerCase().includes(keyword) || item.note.toLowerCase().includes(keyword)
  ))
})

function sourceText(source) {
  return source === 'user_confirmed' ? '用户确认' : '用户输入'
}

function openNewMemory() {
  editing.value = null
  memoryForm.value = { content: '', isSensitive: false }
  editorVisible.value = true
}

function openEditMemory(memory) {
  editing.value = memory
  memoryForm.value = { content: memory.content, isSensitive: memory.isSensitive }
  editorVisible.value = true
}

function saveMemory() {
  const content = memoryForm.value.content.trim()
  if (!content) return
  if (editing.value) {
    editing.value.content = content
    editing.value.isSensitive = memoryForm.value.isSensitive
    editing.value.updatedAt = '刚刚（原型）'
  } else {
    memories.value.unshift({
      id: 'memory-preview-' + Date.now(),
      content,
      note: '新增偏好将在接口接入后保存到你的长期记忆。',
      source: 'user_input',
      updatedAt: '刚刚（原型）',
      isSensitive: memoryForm.value.isSensitive,
    })
  }
  editorVisible.value = false
}

function requestDelete(memory) {
  editing.value = memory
  deleteVisible.value = true
}

function deleteMemory() {
  memories.value = memories.value.filter((item) => item.id !== editing.value?.id)
  deleteVisible.value = false
}

async function signOut() {
  await auth.signOut()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <main class="workspace">
    <aside class="sidebar">
      <div class="brand"><span class="mark">⌾</span>Aegis PA</div>
      <p class="nav-label app-nav-label">工作空间</p>
      <router-link class="nav" :to="{ name: 'tasks' }">◌ 任务对话</router-link>
      <router-link class="nav" :to="{ name: 'mail-management' }">✉ 邮件管理</router-link>
      <router-link class="nav" :to="{ name: 'todo-plans' }">☷ 待办计划</router-link>
      <router-link class="nav" :to="{ name: 'confirmations' }">✓ 操作确认</router-link>
      <router-link class="nav active" :to="{ name: 'memories' }">◇ 长期记忆</router-link>
      <span class="nav disabled">◫ 连接与审计（待接入）</span>

      <div class="identity">
        <b>{{ auth.profile?.name }}</b>
        <span>{{ auth.profile?.email || '已通过 Keycloak 登录' }}</span>
        <el-button text type="primary" @click="signOut">退出登录</el-button>
      </div>
    </aside>

    <section class="content memory-content">
      <header class="page-header">
        <div>
          <h1>长期记忆</h1>
          <p class="muted">一期只保存你手动输入或明确确认的偏好。AI 自动摘要、自动记忆和置信度评分将在后续版本引入。</p>
        </div>
        <el-button type="primary" @click="openNewMemory">新增偏好</el-button>
      </header>

      <article class="card memory-card">
        <div class="card-head memory-toolbar">
          <div class="memory-search">
            <el-input v-model="query" placeholder="搜索已保存的偏好" clearable />
            <span class="badge neutral">共 {{ filteredMemories.length }} 条</span>
          </div>
          <el-button size="small">最近更新</el-button>
        </div>
        <div class="memory-table-wrap">
          <table class="memory-table">
            <thead><tr><th>偏好内容</th><th>来源</th><th>更新时间</th><th>敏感性</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="memory in filteredMemories" :key="memory.id">
                <td><div class="title">{{ memory.content }}</div><div class="detail">{{ memory.note }}</div></td>
                <td><span class="badge read">{{ sourceText(memory.source) }}</span></td>
                <td>{{ memory.updatedAt }}</td>
                <td><span class="badge" :class="memory.isSensitive ? 'sensitive' : 'neutral'">{{ memory.isSensitive ? '敏感' : '普通' }}</span></td>
                <td class="memory-actions">
                  <el-button link type="primary" @click="openEditMemory(memory)">编辑</el-button>
                  <el-button link type="danger" @click="requestDelete(memory)">删除</el-button>
                </td>
              </tr>
              <tr v-if="filteredMemories.length === 0"><td colspan="5" class="memory-empty">没有匹配的长期记忆。</td></tr>
            </tbody>
          </table>
        </div>
      </article>
      <p class="memory-static-note">当前为静态原型：新增、编辑和删除只在本页临时展示，不会调用或写入后端接口。</p>
    </section>

    <el-dialog v-model="editorVisible" :title="editing ? '编辑偏好' : '新增偏好'" width="min(560px, calc(100vw - 32px))">
      <el-form label-position="top">
        <el-form-item label="偏好内容">
          <el-input v-model="memoryForm.content" type="textarea" :rows="4" maxlength="2000" show-word-limit placeholder="例如：周一上午不安排会议" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="memoryForm.isSensitive">此偏好包含敏感信息</el-checkbox>
          <p class="memory-form-help">敏感偏好仅会在你明确授权的场景中使用。</p>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editorVisible = false">取消</el-button>
        <el-button type="primary" :disabled="!memoryForm.content.trim()" @click="saveMemory">保存偏好</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="deleteVisible" title="删除长期记忆" width="min(480px, calc(100vw - 32px))">
      <p>确定删除“{{ editing?.content }}”吗？接口接入后，这一步会创建并核对删除确认。</p>
      <template #footer>
        <el-button @click="deleteVisible = false">取消</el-button>
        <el-button type="danger" @click="deleteMemory">删除</el-button>
      </template>
    </el-dialog>
  </main>
</template>
