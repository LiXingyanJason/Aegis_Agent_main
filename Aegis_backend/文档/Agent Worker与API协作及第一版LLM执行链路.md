# Agent Worker 与 API 协作及第一版 LLM 执行链路

## 1. 文档目的

本文说明 Aegis PA 当前后端中 Web API、Redis 队列、PostgreSQL 与 Python Agent Worker 的职责边界，以及一条用户任务从提交到保存 LLM 回复的完整执行路径。

适用范围为一期当前已经实现的最小链路：**用户发送文本任务 → DeepSeek/OpenAI 兼容模型生成文本回复 → 系统保存回复**。日历、邮件、MCP 工具、SSE 实时推送、审批和 LangGraph 尚未接入这条链路。

## 2. 运行架构

```text
浏览器 / Vue 前端
        │ HTTPS + Bearer access token
        ▼
FastAPI API 服务
  ├─ 验证 Keycloak JWT，映射本地 user_id + tenant_id
  ├─ 写入 PostgreSQL：消息、agent_run
  └─ 数据库事务提交后写入 Redis：run_id
        │
        ▼
Redis 列表队列（aegis:agent-runs）
        │
        ▼
Python Agent Worker
  ├─ 取出 run_id
  ├─ 查询并更新 PostgreSQL 任务状态
  ├─ 调用 DeepSeek / OpenAI 兼容模型
  └─ 写回 Agent 消息、运行步骤、最终状态
        │
        ▼
PostgreSQL
```

API 与 Worker 是两个独立进程，**不通过 HTTP 直接调用彼此**。二者运行时只通过 Redis 和 PostgreSQL 协作：

- Redis 负责通知 Worker “哪一个任务需要处理”，队列中只存储 `run_id`；
- PostgreSQL 是会话、消息、任务状态和模型结果的唯一业务数据来源；
- 前端始终调用 API；后续可通过 `GET /conversations/{conversation_id}` 读取 Worker 已写入的最终回复。

当前无需拆分为两个代码仓库。API 与 Worker 共用 `app/` 下的配置、仓储、数据库事务和领域代码，但在生产环境应部署成两个独立服务。

## 3. 关键服务与启动方式

本地开发需要至少启动 Redis、API 和 Worker。

```powershell
# 终端 1：API
cd D:\AI\Aegis_Agent_main\Aegis_backend
python.exe -m uvicorn app.main:app --reload --port 8000
```

```powershell
# 终端 2：Agent Worker
cd D:\AI\Aegis_Agent_main\Aegis_backend
python.exe -m app.worker.agent_worker
```

Worker 正常启动但没有任务时，会显示：

```text
[Agent Worker] 已启动，正在等待 Redis 队列「aegis:agent-runs」中的任务。
```

`BLPOP` 是 Redis 的阻塞取队列命令。队列为空时 Worker 会阻塞等待，不会高频扫描数据库或持续消耗 CPU；收到任务后才执行模型调用。

## 4. 环境配置

`.env` 至少需要以下配置：

```dotenv
# 连接 Redis；API 和 Worker 必须指向同一个 Redis 数据库。
REDIS_URL=redis://127.0.0.1:6379/0

# Redis 中的任务列表名称；API 和 Worker 必须一致。
AGENT_QUEUE_NAME=aegis:agent-runs

# BLPOP 的整数秒超时；无任务时重新进入阻塞等待。
AGENT_WORKER_POLL_TIMEOUT_SECONDS=5

# OpenAI 兼容模型配置。MODEL_API_KEY 不得提交到 Git。
MODEL_PROVIDER=deepseek
MODEL_API_BASE=https://api.deepseek.com/v1
MODEL_API_KEY=你的真实密钥
MODEL_DEFAULT_NAME=deepseek-chat
MODEL_REQUEST_TIMEOUT_SECONDS=60
```

当前模型客户端支持：

- `openai`；
- `openai_compatible`；
- `deepseek`（使用 OpenAI 兼容的 Chat Completions 请求/响应格式）。

## 5. 用户发送任务的 API 流程

用户调用：

```text
POST /conversations/{conversation_id}/messages
```

请求体：

```json
{
  "content": "帮我整理今天的任务。",
  "client_message_id": "web-message-001"
}
```

处理步骤：

1. `get_current_user` 验证 Bearer access token，并按 `issuer + sub` 映射本地用户；
2. `get_tenant_session` 建立 PostgreSQL 事务，并设置 RLS 所需的 `app.tenant_id`、`app.user_id`；
3. 锁定当前用户拥有的会话，避免同一会话并发分配相同消息序号；
4. 根据 `(conversation_id, client_message_id)` 判断是否为重复请求；
5. 新请求写入一条 `role = user` 的 `conversation_messages`；
6. 创建一条 `agent_runs`，初始状态为 `queued`；
7. 将用户消息关联至新任务，并更新 `conversations.last_message_at`；
8. 数据库事务成功提交后，才将 `run_id` 投递到 Redis 队列；
9. 返回 `202 Accepted`。

响应示例：

```json
{
  "data": {
    "message_id": "...",
    "run_id": "...",
    "status": "queued",
    "idempotent_replay": false
  }
}
```

如果同一会话重复使用相同的 `client_message_id`，不会再次创建消息或任务，而会返回已有任务，`idempotent_replay` 为 `true`。若已有任务仍为 `queued`，API 会再次投递其 `run_id`；Worker 的原子领取机制可防止重复执行。

## 6. 为什么必须在事务提交后投递 Redis

不能在数据库事务尚未提交时让 Worker 取到 `run_id`：此时 Worker 可能查询不到对应的 `agent_runs` 数据。

`app.config.database.tenant_transaction` 提供 `register_after_commit()` 机制。`RunDispatchService.enqueue_after_commit()` 将 Redis `RPUSH` 登记为提交后动作：

```text
数据库事务中：写入消息和 queued run
        │
事务提交成功
        │
        ▼
Redis RPUSH aegis:agent-runs <run_id>
```

这是当前最小实现。后续需要引入 **Transactional Outbox** 与定时补偿任务，以处理“数据库已提交但 Redis 投递失败且用户未重试”的极端场景。

## 7. Agent Worker 执行流程

Worker 入口：`app/worker/agent_worker.py`。

```text
while True
  └─ Redis BLPOP(queue, timeout)
       ├─ 队列为空：继续阻塞等待
       └─ 取得 run_id：调用 AgentOrchestrator.execute(run_id)
```

`AgentOrchestrator` 的最小执行步骤：

1. 使用原子 `UPDATE ... WHERE status = 'queued' RETURNING ...` 领取任务；若没有返回记录，说明任务已经被其他 Worker 领取或不再可执行，结果为 `ignored`；
2. 将 `agent_runs.status` 更新为 `running`，记录实际 `model_provider`、`model_name`、开始时间和当前阶段；
3. 根据 run 中的 `tenant_id + user_id` 建立新的 RLS 租户事务；
4. 查询当前会话最近 20 条 `user`、`assistant` 消息，构造模型上下文；
5. 写入一个 `run_steps` 记录，状态为 `running`，标签为“正在生成回复”；
6. 通过 `OpenAICompatibleClient` 调用 `{MODEL_API_BASE}/chat/completions`；
7. 锁定会话，写入一条 `role = assistant` 的消息；
8. 将运行步骤更新为 `succeeded`，将 `agent_runs` 更新为 `completed`；
9. 发生模型或执行错误时，步骤更新为 `failed`，任务更新为 `failed`，并记录脱敏错误码。

状态值不可混用：

| 表 | 成功状态 | 失败状态 |
|---|---|---|
| `agent_runs` | `completed` | `failed` |
| `run_steps` | `succeeded` | `failed` |

## 8. 数据隔离与安全边界

- API 从 Keycloak token 取得身份，不允许浏览器提交 `tenant_id`、`user_id`；
- API 请求和 Worker 的业务写入均设置 PostgreSQL RLS 租户上下文；
- Worker 不使用浏览器 access token；它通过 `agent_runs` 中已保存的 `tenant_id + user_id` 建立数据库上下文；
- Redis 队列只包含 UUID 格式的 `run_id`，不放邮件正文、会话内容、OIDC token、模型密钥；
- `conversation_messages.content_hash` 保存 SHA-256，用于完整性和后续审计；
- 工具完整请求/响应载荷不会由会话详情接口返回给前端。

## 9. `client_message_id` 幂等约束说明

`client_message_id` 仅用于用户消息的重复发送去重。Agent、系统和工具消息没有该值，因此数据库必须允许同一会话存在多条 `client_message_id IS NULL` 的消息。

新初始化数据库使用的约束是：

```sql
UNIQUE (conversation_id, client_message_id)
```

已有数据库如果曾使用 `UNIQUE NULLS NOT DISTINCT`，必须执行：

```text
scripts/002_fix_message_client_id_unique_constraint.sql
```

该迁移会删除旧约束，并创建仅约束非空 `client_message_id` 的唯一索引：

```sql
CREATE UNIQUE INDEX ux_conversation_messages_client_message_id_present
ON conversation_messages (conversation_id, client_message_id)
WHERE client_message_id IS NOT NULL;
```

## 10. 关键文件索引

| 文件 | 职责 |
|---|---|
| `app/controller/conversation_controller.py` | 会话、历史恢复、发送消息 HTTP 接口 |
| `app/service/conversation_service.py` | 会话及发送消息业务协调 |
| `app/service/run_dispatch_service.py` | 数据库提交后投递 Redis run_id |
| `app/config/database.py` | SQLAlchemy 异步事务、RLS 上下文、提交后回调 |
| `app/worker/agent_worker.py` | Redis 常驻消费循环与本地运行日志 |
| `app/agent/orchestrator.py` | 领取任务、构造上下文、调用模型、保存结果 |
| `app/llm/client.py` | OpenAI/DeepSeek 兼容模型 HTTP 客户端 |
| `app/repository/run_repository.py` | `agent_runs`、`run_steps`、Agent 回复的数据库读写 |
| `app/repository/conversation_repository.py` | 会话、用户消息、会话详情的数据库读写 |
| `scripts/002_fix_message_client_id_unique_constraint.sql` | 已有数据库的消息幂等约束修复 |

## 11. 当前验证与下一步

当前自动化测试覆盖了配置、租户上下文、OIDC 身份映射、会话创建/恢复、发送消息幂等、提交后回调、模型客户端、Agent 最小编排以及任务步骤状态。

运行测试：

```powershell
python.exe -m pytest tests -q
```

当前已验证真实 DeepSeek HTTP 请求能返回 `200 OK`，并已完成一次任务 `completed`。仍需后续实现：

1. `GET /runs/{run_id}` 与 SSE 事件，使页面实时展示运行状态；
2. Transactional Outbox、重试和超时恢复；
3. Prompt Injection 防护、输出脱敏与工具权限策略；
4. MCP 工具调用、工具预览和逐项审批；
5. 多 Worker 并发、监控、日志聚合和告警；
6. 复杂任务状态机或 LangGraph 编排。
