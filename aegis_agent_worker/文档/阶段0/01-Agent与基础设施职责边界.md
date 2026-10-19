# Agent 与基础设施职责边界

## 核心划分

```text
agent/
  = AI 决策与任务流程

agent 外
  = 可复用业务能力、基础设施与持久化
```

`agent/` 决定“本任务下一步做什么”；它不直接编写 SQL、不直接管理事务，也不直接实现 MCP 网络通信。

## 目录职责

| 目录 | 主要职责 |
| --- | --- |
| `agent/` | 图结构、节点与子图；意图/工作流路由；专职 Agent 的 Prompt 与推理策略；多 Agent 协同与交接；任务下一步决策。 |
| `service/` | 任务状态迁移、会话上下文装配、工具调用审计与事件编排；后续草稿、审批、恢复执行；事务边界。 |
| `repository/` | SQL 与数据库读写；包括 `agent_runs`、`run_steps`、`run_events`、`tool_calls` 等。 |
| `llm/` | 模型供应商通信适配：DeepSeek、OpenAI 等 API；超时、响应解析、错误转换。它不决定业务流程。 |
| `tool/` | Tool 契约、Tool Gateway、MCP Client、参数校验与外部服务调用。 |
| `event/` | Redis 事件协议及事件发布，使主后端可通过 SSE 向前端转发进度。 |
| `worker/` | Redis 队列消费循环与 Worker 进程启动入口。 |

## 调用实例：用户查询“我明天有什么日程？”

```text
前端提交消息
  ↓
Aegis_backend
  保存用户消息与 agent_run，提交后将 run_id 推入 Redis 队列
  ↓
worker/agent_worker.py
  从 Redis 取出 run_id，调用 agent/orchestrator.py
  ↓
agent/
  识别为“日历只读查询”，选择日历查询流程和 calendar.list_events 工具
  ↓
service/
  将任务置为 running；创建 run_step、tool_call 审计记录；登记进度事件
  ↓
tool/
  Tool Gateway 校验参数，经 MCP Client 调用 Calendar MCP Server
  ↓
service/ + repository/
  保存工具结果、完成步骤和任务状态；持久化 run_events
  ↓
event/
  将已提交的进度事件发布到 Redis Pub/Sub
  ↓
Aegis_backend SSE
  读取持久化事件并订阅实时事件，向前端展示工具预览、进度与最终回答
```

在这条链路中，`agent/` 只负责识别任务与选择下一步；任务记录、权限/策略、事务、工具审计和事件发布由 `service/` 等外围层负责。因此，同一日历工具也可以被后续的会议安排、冲突检查等不同工作流复用。
