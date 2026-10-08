aegis_agent_worker/文档# Agent 图与工作流架构

> 当前已实现根图、日历读取与会议确认流程、邮件摘要/回复草稿/发送确认子图，以及基于 `run_id` 的审批暂停与恢复。意图路由目前仍是确定性规则；LLM 受限路由与多 Agent 协同是后续能力。

## 建议目录

```text
agent/
├── graph/
│   ├── task_graph.py               # 根图：整体入口与主路由
│   ├── state.py                    # 全局 TaskGraphState
│   ├── graph_runner.py             # invoke / resume，thread_id = run_id
│   ├── graph_registry.py           # 注册可用子图
│   └── router.py                   # 根图路由规则
│
├── workflows/
│   ├── calendar/
│   │   ├── factory.py              # 组装日程专家、应用服务与日历子图
│   │   ├── read_graph.py
│   │   ├── confirmation_graph.py
│   │   ├── state.py
│   │   └── nodes/
│   ├── mail/
│   │   ├── factory.py              # 组装邮件领域 Service 与邮件子图
│   │   ├── extraction_graph.py
│   │   ├── reply_draft_graph.py
│   │   ├── send_confirmation_graph.py
│   │   └── nodes/
│   └── shared/
│       ├── approval_nodes.py
│       ├── error_nodes.py
│       └── result_nodes.py
│
├── specialists/
│   ├── calendar_agent.py
│   ├── mail_agent.py
│   └── research_agent.py
│
└── routing/
    ├── intent_router.py
    ├── workflow_router.py
    ├── risk_router.py
    └── approval_router.py
```

## 各层职责

| 模块 | 职责 |
| --- | --- |
| `graph/` | 根图及其稳定的全局状态。只负责一次任务的统一入口、按 `run_type` / 路由结果选择已注册子图及恢复执行；`thread_id` 使用 `run_id`。 |
| `workflows/` | 按业务领域组织的子图。领域 `factory.py` 自行组装其 Agent、Service 与子图，并注册到根图；一个 `*_graph.py` 通常对应一个可编译流程。 |
| `workflows/shared/` | 跨业务流程复用的节点，例如审批、错误归一化、结果整理；它们通常不是独立子图。 |
| `specialists/` | 具有专属 Prompt、工具范围和推理策略的专业 Agent，例如日程助手、邮件助手、研究助手。 |
| `routing/` | 可复用的判断规则：识别用户意图、选择工作流、评估风险、根据批准/拒绝结果选择后续路径。根图调用它们，而不是在根图中堆积判断代码。 |

## 根图的两类入口

根图按**任务参数是否已经明确**区分入口，而不是按“这是邮件还是日历”区分。两个入口可以进入同一个领域子图：

```text
START
  └─ select_run_workflow
       ├─ 已明确指定任务对象的后台任务
       │    ├─ 邮件摘要 / 起草 / 发送确认 → 邮件子图
       │    └─ 日历确认恢复 → 日历子图
       │
       └─ 普通对话任务
            └─ load_context → decide_workflow
                 ├─ 邮件查询、摘要、起草 → 邮件子图
                 ├─ 日历查询、会议安排 → 日历子图
                 └─ 普通回复 → generate_reply → END
```

“已明确指定任务对象”通常来自页面按钮：例如用户对某封邮件点击“生成摘要”，主后端已创建携带 `mail_message_id` 的 `mail_extraction` 任务；根图依据受信任的 `run_type` 直接进入邮件子图，不再由模型重复猜测意图。审批恢复也同理：`run_id`、审批决定和已保存的 Checkpoint 已确定，直接恢复相应子图。

“普通对话任务”只有自然语言，因此必须先加载会话上下文、识别意图，并在必要时检索或让用户选择目标对象。例如“帮我总结本周续约邮件”应先通过邮件查询确定候选邮件和 `mail_message_id`，再复用邮件摘要子图；不能把缺少邮件标识的聊天状态直接送入页面按钮所创建的 `mail_extraction` 子图。

当前实现已具备：对话 → 日历读取/会议安排/普通回复，以及页面后台任务 → 邮件摘要、回复起草、发送确认。普通对话 → 邮件查询、摘要、起草是下一步要补齐的“查询/选择邮件对象 + 路由”链路；在此完成前，聊天中的邮件请求会安全回退为普通回复，不会误执行缺少目标邮件的后台子图。工具审计、事务、SSE 预览仍由应用 Service 处理。

## 邮件子图的节点边界

邮件摘要和回复草稿都采用与日历确认相同的边界：LangGraph 节点负责步骤顺序，领域 Service 负责受控业务动作。

```text
邮件摘要：ensure_mail_extraction_ready → generate_mail_extraction → END
邮件起草：ensure_mail_reply_draft_ready → generate_mail_reply_draft → END
```

预检节点只确认当前 `run_id` 对应的邮件任务、用户及租户归属仍然有效。生成节点通过 `MailTaskService` 经 `ToolGateway` 读取邮件，以局部变量调用 `MailAgent` 完成 LLM 推理和结构化输出校验，最后再调用 `MailTaskService` 保存摘要/待办或回复草稿并发布 SSE 事件。邮件正文、模型原始输出和草稿正文不会作为图状态返回，因此不会被写入 LangGraph Checkpoint；图状态仅保存阶段标记和完成标记。

```text
MailExtractionWorkflow
  → MailTaskService.load_extraction_source / read_mail
  → MailAgent.generate_extraction              # Prompt、LLM 调用、JSON 校验
  → MailTaskService.save_extraction

MailReplyDraftWorkflow
  → MailTaskService.load_reply_source / read_mail
  → MemoryContextService.select_for_run        # 仅起草时加载相关偏好
  → MailAgent.generate_reply_draft             # Prompt、LLM 调用、JSON 校验
  → MailTaskService.save_reply_draft
```

Worker 是当前的组合根：它创建数据库、工具网关、Repository 与领域 Service，再创建各领域 Factory 并传给 `AgentOrchestrator`。`TaskGraph` 与 `AgentOrchestrator` 不再创建 Calendar 或 Mail 专属 Service；新增领域只需追加一个实现 `WorkflowRegistrar` 的 Factory。

## Service 分层

`service/` 不承载 Agent 推理策略，而是提供工作流可调用的业务与基础设施动作。按领域与跨领域能力分层如下：

```text
service/
├── runtime/                         # 任务生命周期、会话上下文
├── calendar/                        # 日历确认等日历领域动作
├── mail/                            # 邮件提取、草稿、发送确认
├── memory/                          # 长期记忆选择与提示词上下文
└── tool/                            # 工具执行、审计、事务和 MCP 调用编排
```

Agent 和 Workflow 可以依赖 Service；Service 不应反向依赖具体 Workflow 或 Specialist Agent。这样邮件、日历等领域服务可以在页面后台任务与对话子图之间复用。

## 调用实例：安排与王敏的项目同步

```text
用户：“帮我安排明天下午与王敏进行 30 分钟项目同步。”
  ↓
graph/task_graph.py
  初始化 TaskGraphState，使用 run_id 作为 thread_id
  ↓
routing/intent_router.py
  识别意图：创建会议
  ↓
routing/workflow_router.py
  选择 workflows/calendar/confirmation_graph.py
  ↓
calendar/confirmation_graph.py
  ├─ 由 calendar_agent.py 提取参会人、时长、时间范围
  ├─ 调用日历读取工具查询双方空闲时间
  ├─ 生成会议草稿和只读预览
  └─ 调用 shared/approval_nodes.py 创建待确认项并暂停
  ↓
用户在确认页批准
  ↓
graph/graph_runner.py
  以相同 thread_id = run_id 恢复图
  ↓
routing/approval_router.py
  批准 → 执行创建会议；拒绝/过期 → 结束或返回修改
  ↓
shared/result_nodes.py
  归一化最终结果，交由外部 service/ 持久化并发布 SSE 事件
```

## 状态边界

- 根图状态只保存跨流程稳定的信息：`run_id`、用户/租户标识、当前工作流、执行状态、审批项标识和结果摘要。
- 子图可拥有局部状态，例如日历候选时段、会议草稿或邮件草稿。
- 用户、日程、草稿、审批和审计等业务事实始终以数据库为准；图状态只保存流程执行所需的数据和引用标识。
