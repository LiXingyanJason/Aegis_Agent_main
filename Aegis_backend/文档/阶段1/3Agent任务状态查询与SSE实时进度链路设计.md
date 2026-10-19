# Agent 任务状态查询与 SSE 实时进度链路设计

## 1. 文档目的

本文说明 Aegis PA 如何查询一次 Agent 任务的状态，以及如何将任务执行进度、最终回复和失败信息实时推送到前端。

适用范围是当前已实现的文本任务链路与一期只读日历 MCP 工具：用户提交消息后，Agent Worker 可在识别到日程意图时查询日程或可用时间，保存工具记录、调用 LLM 并保存回复。邮件、日历写入和审批等后续能力可复用本设计，但尚未接入当前事件流。
## 2. 核心概念

### 2.1 `conversation_id` 与 `run_id`

- `conversation_id`：一段对话的唯一标识；一段对话可包含多条用户消息和多次 Agent 执行。
- `run_id`：一次 Agent 任务执行的唯一标识；用户每次发送一条需要 Agent 处理的消息，通常会产生一个新的 `run_id`。

当前 SSE 以 `run_id` 为范围：

```text
一个 run_id = 一次 Agent 执行 = 一条短生命周期 SSE 连接
```

任务完成或失败后，该 SSE 连接结束。用户在同一会话再次提交任务时，前端获得新的 `run_id`，并建立新的 SSE 订阅。

### 2.2 两种查询方式

| 方式 | 接口 | 用途 |
|---|---|---|
| 状态快照查询 | `GET /runs/{run_id}` | 页面刷新、恢复任务状态、SSE 不可用时的轮询兜底 |
| 实时事件订阅 | `GET /runs/{run_id}/events` | 接收任务状态变化、进度、回复和终态事件 |

两者并存：SSE 提供低延迟体验，数据库状态查询提供可恢复的权威快照。

## 3. 组件职责

```text
Vue 前端
  ├─ GET /runs/{run_id}：读取任务状态快照
  └─ GET /runs/{run_id}/events：订阅 SSE
                 │
                 ▼
FastAPI API
  ├─ 验证 Keycloak Bearer Token
  ├─ 校验 run_id 归属当前 tenant_id + user_id
  ├─ 从 PostgreSQL 补发 run_events 历史事件
  └─ 订阅 Redis Pub/Sub 并转发为 SSE
                 ▲
                 │ 实时通知
Redis Pub/Sub：aegis:run:{run_id}:events
                 ▲
                 │ 提交后发布
Agent Worker / AgentOrchestrator
  ├─ 更新 agent_runs、run_steps、conversation_messages
  └─ 在同一事务内写入 run_events
                 │
                 ▼
PostgreSQL：agent_runs、run_steps、conversation_messages、run_events
```

职责边界如下：

- PostgreSQL 是任务状态、消息和事件的唯一业务事实来源。
- Redis Pub/Sub 只承担低延迟通知，不保存事件历史。
- Worker 不直接向浏览器推送；浏览器始终连接 FastAPI API。
- 前端不直接访问 Redis 或数据库。

## 4. 数据模型与事件顺序

`run_events` 表持久保存任务事件，关键字段包括：

| 字段 | 含义 |
|---|---|
| `run_id` | 所属任务 |
| `tenant_id` | 租户隔离条件 |
| `event_no` | 同一任务内递增的事件编号，用于排序、去重和断线续传 |
| `event_type` | 事件业务类型 |
| `payload` | 事件具体数据，使用 JSONB 保存 |
| `created_at` | 事件创建时间 |

当前第一版实际发送以下事件：

| 事件类型 | 产生时机 | `payload` 关键字段 | 前端动作 |
|---|---|---|---|
| `run_started` | Worker 原子领取 `queued` 任务后 | `status`、`current_stage`、模型提供方、模型名 | 标记任务开始执行 |
| `progress_updated` | 创建“正在生成回复”执行步骤后 | `step_id`、`label`、`status` | 更新右侧执行进度 |
| `assistant_message_completed` | LLM 回复已写入数据库后 | `content`、`is_final` | 在对话区显示完整 Agent 回复 |
| `run_completed` | 任务成功完成后 | `status`、`result_summary` | 标记完成、关闭 SSE |
| `run_failed` | LLM 调用或执行失败后 | `status`、`error_code`、`error_message` | 显示失败信息、关闭 SSE |
| `tool_preview` | 只读工具调用成功或失败后 | 工具名、风险、输入/输出摘要或错误 | 展示工具预览 |
| `connection_required` | 缺少有效日历连接时 | 服务、所需 scope、提示 | 展示连接提示 |

当前采用统一的 `RunEvent` 数据结构，通过 `event_type` 字段区分事件，而不是为每种事件定义独立 Python 类。SSE 输出时，`event_type` 会被写到 SSE 的 `event:` 字段。

## 5. Worker 的事件写入与发布顺序

Worker 的任务执行过程遵守以下原则：

```text
先在 PostgreSQL 提交业务状态与 run_events
再通过 Redis Pub/Sub 发布实时通知
```

成功任务的简化时序如下：

```text
Worker 领取 queued run
  → PostgreSQL：agent_runs = running，写入 run_started，提交
  → Redis Pub/Sub：发布 run_started

创建 LLM 执行步骤
  → PostgreSQL：run_steps = running，写入 progress_updated，提交
  → Redis Pub/Sub：发布 progress_updated

调用 LLM
  → PostgreSQL：写入 assistant 消息、run_steps = succeeded、agent_runs = completed
                  写入 assistant_message_completed、run_completed，提交
  → Redis Pub/Sub：依次发布上述两个事件
```

失败时，Worker 将步骤和任务更新为失败，在同一事务写入 `run_failed`，提交后再发布通知。

`RunEventPublisher.publish_after_commit()` 使用数据库的提交后回调机制，确保 SSE 永远不会收到尚未提交的事件。若 Redis Pub/Sub 发布失败，事件仍已在 PostgreSQL 中，后续 SSE 重连可补发；实时通知故障不会中断 LLM 任务。

Redis 频道名由 `run_event_channel(run_id)` 统一生成：

```text
aegis:run:{run_id}:events
```

Redis Pub/Sub 频道无需预先创建。`subscribe(channel)` 的含义是开始监听这个名称对应的频道；`publish(channel, message)` 会将消息转交给当前所有订阅者。

## 6. SSE 接口建立流程

接口：

```http
GET /runs/{run_id}/events
Authorization: Bearer <access_token>
Last-Event-ID: <event_no，可选>
```

`stream_run_events()` 的执行步骤：

1. `get_current_user` 验证 Keycloak access token，取得本地 `user_id` 与 `tenant_id`。
2. 解析可选的 `Last-Event-ID`；未提供时按 `0` 处理。
3. 在租户事务中查询 `agent_runs`，确认该 `run_id` 属于当前用户。
4. 任务不存在或不属于当前用户时统一返回 `404 Not Found`，避免泄露其他用户任务是否存在。
5. 返回 `StreamingResponse`，将后续 SSE 输出交给 `_run_event_stream()` 异步生成器。

接口响应类型为：

```http
Content-Type: text/event-stream
```

并设置 `Cache-Control: no-cache`、`Connection: keep-alive` 与 `X-Accel-Buffering: no`，防止浏览器、代理或 Nginx 缓冲事件流。

## 7. 历史补发、实时订阅与去重

`_run_event_stream()` 的关键顺序为：

```text
1. Redis Pub/Sub subscribe(aegis:run:{run_id}:events)
2. PostgreSQL 查询 event_no > Last-Event-ID 的 run_events
3. 依序 SSE 推送历史事件
4. 持续读取 Redis Pub/Sub 新消息并 SSE 推送
```

必须先订阅 Redis 再查询历史数据库事件。若先读历史、后订阅，二者之间刚产生的事件可能既不在历史查询结果中，也未被实时订阅接收，造成遗漏。

订阅建立后、历史查询完成前到达的 Redis 消息会在 Pub/Sub 缓冲中等待读取。服务端通过 `last_sent_event_no` 去重：编号小于或等于已经补发的历史事件不会再次推送。

SSE 输出格式示例：

```text
id: 3
event: progress_updated
data: {"run_id":"...","event_no":3,"payload":{"label":"正在生成回复","status":"running"},"created_at":"2026-10-04T08:00:00+00:00"}

```

`id` 就是 `event_no`。前端成功处理事件后保存最新编号；断线重连时在 `Last-Event-ID` 传回该编号，服务端只补发后续事件。

当读取到 `run_completed` 或 `run_failed` 时，服务端结束生成器并关闭 SSE 连接。这两个事件是当前的终态事件。

若 15 秒没有 Redis 新消息，服务端发送：

```text
: keepalive

```

这是 SSE 注释心跳，浏览器不作为业务事件处理，但可以降低中间代理因空闲而关闭连接的风险。

无论任务结束、用户断开页面还是请求被取消，`finally` 都会执行 Redis `unsubscribe()` 和 `aclose()`，防止 Pub/Sub 连接泄漏。

## 8. 前端接入要求

前端在 `POST /conversations/{conversation_id}/messages` 成功后取得 `run_id`，再订阅对应事件流。

浏览器原生 `EventSource` 不能在请求中设置 `Authorization` Header；而当前后端认证要求 Bearer Token。因此前端必须使用 `fetch` + `ReadableStream` 请求 SSE 接口：

```text
fetch(/runs/{run_id}/events, {
  headers: {
    Authorization: Bearer <access_token>,
    Last-Event-ID: <latest_event_no>
  }
})
```

不得将 access token 拼接到 URL 查询参数中。前端应：

1. 按 SSE 规范解析 `id`、`event`、`data`；
2. 成功处理事件后保存该任务的最新 `event_no`；
3. 收到 `run_completed` 或 `run_failed` 时关闭读取流并停止该任务轮询；
4. 页面卸载、切换会话或发起替代订阅时，用 `AbortController` 主动中止旧请求；
5. SSE 不可用、页面刷新或恢复历史会话时，使用 `GET /runs/{run_id}` 与 `GET /conversations/{conversation_id}` 作为兜底。

## 9. 安全与隔离

- 前端不能自行指定 `tenant_id` 或 `user_id`；身份完全从 Bearer Token 映射。
- SSE 建立前必须检查任务归属，查询条件包含 `run_id + tenant_id + user_id`。
- 事件历史查询必须带 `tenant_id` 限制。
- Redis 频道不包含邮件正文、会话内容、OIDC token 或模型密钥；实时消息只包含已脱敏、适合页面展示的事件载荷。
- 对“不存在”和“无权访问”的任务统一返回 `404`，避免枚举任务 ID。

## 10. 关键代码索引

| 文件 | 职责 |
|---|---|
| `app/controller/run_controller.py` | `GET /runs/{run_id}` 与 SSE 接口、SSE 帧格式化、连接清理 |
| `app/service/run_service.py` | 任务归属校验与事件读取业务协调 |
| `app/repository/run_repository.py` | `agent_runs`、`run_steps`、`run_events` 的查询和写入 |
| `app/event/run_event.py` | `RunEvent`、频道命名和 Redis 事件发布器 |
| `app/agent/orchestrator.py` | Agent 状态变更、事件写入、提交后发布 |
| `app/worker/agent_worker.py` | 从 Redis 列表队列消费 `run_id` 并执行编排 |
| `app/config/database.py` | 租户事务、RLS 上下文与提交后回调 |
| `tests/event/test_run_event.py` | 事件序列化、SSE 格式与 Last-Event-ID 测试 |

## 11. 已知边界与后续演进

当前实现适合一期单任务文本 Agent 链路。后续可在保持 `RunEvent` 与 `event_no` 机制不变的前提下扩展：

1. LLM token 级 `assistant_message_delta` 流式输出；
2. 更多 MCP 工具（邮件等）及工具调用重试、限流和熔断；
3. 审批相关事件，例如 `approval_required`、`approval_executed`；
4. Transactional Outbox、事件发布重试和死信监控；
5. 聚合式会话事件流或全局通知流，用于多个并行 `run_id`；
6. 事件载荷版本号与兼容策略，避免前后端独立发布时破坏解析逻辑。

在新增事件类型时，应同步更新后端事件定义、前端分发逻辑、接口文档和测试用例。
