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
    runEventNos: {},
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
            steps: [],
            tool_previews: [],
            approval_items: [],
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
    applyRunEvent(event) {
      const runId = event.run_id
      const eventNo = Number(event.event_no)
      if (!runId || !Number.isSafeInteger(eventNo)) return false
      if (eventNo <= (this.runEventNos[runId] ?? 0)) return false

      const runIndex = this.runs.findIndex((run) => run.run_id === runId)
      if (runIndex === -1) return false

      const payload = event.payload ?? {}
      const run = {
        ...this.runs[runIndex],
        steps: [...(this.runs[runIndex].steps ?? [])],
        tool_previews: [...(this.runs[runIndex].tool_previews ?? [])],
        approval_items: [...(this.runs[runIndex].approval_items ?? [])],
        plan_previews: [...(this.runs[runIndex].plan_previews ?? [])],
      }

      if (event.event_type === 'run_started') {
        run.status = payload.status ?? 'running'
        run.current_stage = payload.current_stage ?? run.current_stage
        run.model_provider = payload.model_provider ?? run.model_provider
        run.model_name = payload.model_name ?? run.model_name
      }

      if (event.event_type === 'progress_updated') {
        run.status = run.status === 'queued' ? 'running' : run.status
        const stepIndex = run.steps.findIndex((step) => step.step_id === payload.step_id)
        const step = {
          ...(stepIndex === -1 ? {} : run.steps[stepIndex]),
          step_id: payload.step_id,
          label: payload.label,
          status: payload.status,
          detail: payload.detail ?? null,
          started_at: payload.occurred_at ?? null,
        }
        if (stepIndex === -1) run.steps.push(step)
        else run.steps.splice(stepIndex, 1, step)
      }

      if (event.event_type === 'tool_preview' && payload.tool_call_id) {
        const previewIndex = run.tool_previews.findIndex(
          (tool) => tool.tool_call_id === payload.tool_call_id,
        )
        const preview = {
          ...(previewIndex === -1 ? {} : run.tool_previews[previewIndex]),
          tool_call_id: payload.tool_call_id,
          tool_name: payload.tool_name,
          risk_level: payload.risk_level,
          input_summary: payload.input_summary ?? null,
          output_summary: payload.output_summary ?? null,
          status: payload.status,
          error_code: payload.error_code ?? null,
          error_message: payload.error_message ?? null,
        }
        if (previewIndex === -1) run.tool_previews.push(preview)
        else run.tool_previews.splice(previewIndex, 1, preview)
      }

      if (event.event_type === 'connection_required') {
        run.current_stage = '需要连接日历'
        run.connection_required = {
          provider: payload.provider,
          required_scopes: payload.required_scopes ?? [],
          message: payload.message,
        }
      }

      if (event.event_type === 'plan_preview' && payload.preview) {
        const previewIndex = run.plan_previews.findIndex(
          (preview) => preview.approval_item_id === payload.approval_item_id,
        )
        const preview = {
          ...(previewIndex === -1 ? {} : run.plan_previews[previewIndex]),
          ...payload,
        }
        if (previewIndex === -1) run.plan_previews.push(preview)
        else run.plan_previews.splice(previewIndex, 1, preview)
      }

      if (event.event_type === 'approval_required') {
        const approvalItemIds = payload.approval_item_ids
          ?? (payload.approval_item_id ? [payload.approval_item_id] : [])
        for (const approvalItemId of approvalItemIds) {
          if (!run.approval_items.some((item) => item.approval_item_id === approvalItemId)) {
            run.approval_items.push({
              approval_item_id: approvalItemId,
              action: payload.action ?? 'calendar.create_event',
              risk_level: payload.risk_level ?? 'write',
              title: payload.title ?? '待确认的日历操作',
              status: 'pending',
              resource_type: payload.resource_type ?? null,
              resource_id: payload.resource_id ?? null,
              preview_snapshot: payload.preview ?? null,
            })
          }
        }
        run.approval_required = {
          ...payload,
          approval_item_ids: approvalItemIds,
        }
        run.current_stage = '等待逐项确认'
        // payload.status 描述的是 approval_item（通常为 pending），不能覆盖任务运行状态。
        run.status = 'waiting_confirmation'
      }

      if (event.event_type === 'approval_executed') {
        const approvalIndex = run.approval_items.findIndex(
          (item) => item.approval_item_id === payload.approval_item_id,
        )
        if (approvalIndex !== -1) {
          run.approval_items.splice(approvalIndex, 1, {
            ...run.approval_items[approvalIndex],
            status: payload.status ?? 'executed',
            provider_resource_id: payload.provider_resource_id ?? null,
          })
        }
        run.current_stage = '日历事件已创建'
        run.approval_execution = {
          approval_item_id: payload.approval_item_id,
          provider_resource_id: payload.provider_resource_id ?? null,
          status: payload.status ?? 'executed',
        }
      }

      if (event.event_type === 'assistant_message_completed' && payload.content) {
        const alreadyPresent = this.messages.some(
          (message) => message.role === 'assistant'
            && message.run_id === runId
            && message.content === payload.content,
        )
        if (!alreadyPresent) {
          this.messages.push({
            message_id: 'sse-' + runId + '-' + eventNo,
            role: 'assistant',
            content: payload.content,
            created_at: event.created_at ?? new Date().toISOString(),
            run_id: runId,
            is_final: payload.is_final ?? true,
          })
        }
      }

      if (event.event_type === 'run_completed') {
        run.status = payload.status ?? 'completed'
        run.current_stage = '已完成'
        run.result_summary = payload.result_summary ?? run.result_summary
      }

      if (event.event_type === 'run_failed') {
        run.status = payload.status ?? 'failed'
        run.current_stage = '执行失败'
        run.error_code = payload.error_code ?? run.error_code
        run.error_message = payload.error_message ?? payload.message ?? run.error_message
      }

      this.runs.splice(runIndex, 1, run)
      this.runEventNos[runId] = eventNo
      if (this.latestSubmission?.run_id === runId) {
        this.latestSubmission = {
          ...this.latestSubmission,
          status: run.status,
        }
      }
      return true
    },
  },
})
