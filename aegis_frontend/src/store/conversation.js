import { defineStore } from 'pinia'
import {
  createConversation,
  getConversationDetail,
  listConversations,
  sendConversationMessage,
} from '../api/conversation'
import { getRunDetail } from '../api/run'

export const useConversationStore = defineStore('conversation', {
  state: () => ({
    history: [],
    current: null,
    messages: [],
    runs: [],
    latestSubmission: null,
    loadingHistory: false,
    loadingDetail: false,
    creating: false,
    sending: false,
  }),
  actions: {
    async loadHistory() {
      this.loadingHistory = true
      try {
        this.history = await listConversations()
        return this.history
      } finally {
        this.loadingHistory = false
      }
    },
    async loadConversation(conversationId) {
      this.loadingDetail = true
      try {
        const detail = await getConversationDetail(conversationId)
        this.current = detail
        this.messages = detail.messages ?? []
        this.runs = detail.runs ?? []
        this.latestSubmission = null
        return detail
      } finally {
        this.loadingDetail = false
      }
    },
    async start(title) {
      this.creating = true
      try {
        this.current = await createConversation(title)
        this.messages = []
        this.runs = []
        this.latestSubmission = null
        this.history.unshift({
          ...this.current,
          last_message_at: null,
          latest_message_preview: null,
        })
        return this.current
      } finally {
        this.creating = false
      }
    },
    async send(content, clientMessageId) {
      if (!this.current) throw new Error('会话尚未准备完成。')
      this.sending = true
      try {
        const result = await sendConversationMessage(
          this.current.conversation_id,
          content,
          clientMessageId,
        )
        if (!this.messages.some((message) => message.message_id === result.message_id)) {
          this.messages.push({
            message_id: result.message_id,
            role: 'user',
            content,
            created_at: new Date().toISOString(),
            run_id: result.run_id,
            is_final: true,
          })
        }
        if (!this.runs.some((run) => run.run_id === result.run_id)) {
          this.runs.unshift({
            run_id: result.run_id,
            status: result.status,
            current_stage: '等待处理',
          })
        }
        this.latestSubmission = result
        const historyIndex = this.history.findIndex(
          (conversation) => conversation.conversation_id === this.current.conversation_id,
        )
        if (historyIndex !== -1) {
          const [conversation] = this.history.splice(historyIndex, 1)
          this.history.unshift({
            ...conversation,
            last_message_at: new Date().toISOString(),
            latest_message_preview: content,
          })
        }
        return result
      } finally {
        this.sending = false
      }
    },
    async refreshRun(runId) {
      const run = await getRunDetail(runId)
      if (this.current?.conversation_id === run.conversation_id) {
        const index = this.runs.findIndex((item) => item.run_id === run.run_id)
        if (index === -1) {
          this.runs.unshift(run)
        } else {
          this.runs.splice(index, 1, {
            ...this.runs[index],
            ...run,
          })
        }
      }
      if (this.latestSubmission?.run_id === run.run_id) {
        this.latestSubmission = {
          ...this.latestSubmission,
          status: run.status,
        }
      }
      return run
    },
  },
})
