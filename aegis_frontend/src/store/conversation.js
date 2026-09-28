import { defineStore } from 'pinia'
import { createConversation, sendConversationMessage } from '../api/conversation'

export const useConversationStore = defineStore('conversation', {
  state: () => ({
    current: null,
    messages: [],
    creating: false,
    sending: false,
  }),
  actions: {
    async start(title) {
      this.creating = true
      try {
        this.current = await createConversation(title)
        this.messages = []
        return this.current
      } finally {
        this.creating = false
      }
    },
    async send(content) {
      if (!this.current) throw new Error('会话尚未准备完成。')
      this.sending = true
      try {
        const result = await sendConversationMessage(this.current.conversation_id, content)
        this.messages.push({ role: 'user', content })
        return result
      } finally {
        this.sending = false
      }
    },
  },
})
