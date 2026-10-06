# Agent 图与工作流架构

> 当前已实现根图、日历读取子图、确定性意图/工作流路由和日程专职 Agent。邮件工作流、完整审批恢复链路及多 Agent 协同仍是后续能力。

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
│   │   ├── read_graph.py
│   │   ├── confirmation_graph.py
│   │   ├── state.py
│   │   └── nodes/
│   ├── mail/
│   │   ├── draft_graph.py
│   │   ├── send_confirmation_graph.py
│   │   └── nodes/
│   └── shared/
│       ├── approval_nodes.py
│       ├── error_nodes.py
│       └── result_nodes.py
│
├── specialists/
│   ├── scheduler_agent.py
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
| `graph/` | 根图及其稳定的全局状态。负责一次任务的统一入口、恢复执行和子图调度。`thread_id` 使用 `run_id`，使一次 Agent 执行可恢复、可追踪。 |
| `workflows/` | 按业务领域组织的子图。一个 `*_graph.py` 通常对应一个可编译、可被根图调用的完整流程。`nodes/` 放该流程专属节点。 |
| `workflows/shared/` | 跨业务流程复用的节点，例如审批、错误归一化、结果整理；它们通常不是独立子图。 |
| `specialists/` | 具有专属 Prompt、工具范围和推理策略的专业 Agent，例如日程助手、邮件助手、研究助手。 |
| `routing/` | 可复用的判断规则：识别用户意图、选择工作流、评估风险、根据批准/拒绝结果选择后续路径。根图调用它们，而不是在根图中堆积判断代码。 |

当前只读日历的实际执行路径为：`TaskGraph → load_context → decide_workflow → calendar_read 子图 → generate_reply`。其中 `calendar_read` 子图仅选择并调用白名单中的只读工具；工具审计、事务、SSE 预览仍由 `ToolExecutionService` 处理。

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
  ├─ 由 scheduler_agent.py 提取参会人、时长、时间范围
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
