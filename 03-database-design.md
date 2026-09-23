# Aegis PA 数据库设计

## 1. 存储分工

| 存储 | 数据 | 生命周期 |
|---|---|---|
| Redis | 会话窗口、LangGraph checkpoint、确认状态、限流、幂等结果 | TTL 30 分钟～24 小时 |
| PostgreSQL | 用户、连接、会话、消息摘要、实体、任务、审计 | 业务留存策略控制 |
| pgvector | 长期记忆的 embedding | 与所属记忆同生命周期 |

所有 PostgreSQL 业务表含 `tenant_id`；应用层强制租户过滤，生产环境建议启用 Row Level Security（RLS）。主键均为 UUID，时间字段为 `timestamptz`。

## 2. 核心 ER 关系

```text
users 1─* conversations 1─* messages
users 1─* memories ─* memory_entities *─1 entities
users 1─* provider_connections
conversations 1─* agent_runs 1─* tool_calls
agent_runs 1─* audit_events
```

## 3. PostgreSQL 表定义

### `users`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | uuid PK | 用户 ID |
| tenant_id | uuid | 租户 ID，索引 |
| external_subject | text | 身份提供商 subject，租户内唯一 |
| timezone | text | 默认时区 |
| locale | text | 语言偏好 |
| created_at / updated_at | timestamptz | 审计时间 |

### `conversations` 与 `messages`

`conversations(id, tenant_id, user_id, title, status, created_at, updated_at)`：索引 `(tenant_id, user_id, updated_at DESC)`。

`messages(id, conversation_id, role, content_ciphertext, content_redacted, token_count, created_at)`：原文加密保存；日志只可使用 `content_redacted`。索引 `(conversation_id, created_at)`。

### `memories`

```sql
CREATE TABLE memories (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  user_id uuid NOT NULL REFERENCES users(id),
  type text NOT NULL CHECK (type IN ('summary','preference','fact','episodic')),
  content_ciphertext bytea NOT NULL,
  content_redacted text NOT NULL,
  embedding vector(1536),
  source_message_id uuid REFERENCES messages(id),
  confidence numeric(3,2) NOT NULL DEFAULT 0.80,
  importance numeric(3,2) NOT NULL DEFAULT 0.50,
  access_count integer NOT NULL DEFAULT 0,
  last_accessed_at timestamptz,
  expires_at timestamptz,
  is_sensitive boolean NOT NULL DEFAULT false,
  deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memories_scope_idx ON memories (tenant_id, user_id, type, created_at DESC)
  WHERE deleted_at IS NULL;
CREATE INDEX memories_embedding_idx ON memories USING hnsw (embedding vector_cosine_ops)
  WHERE deleted_at IS NULL;
```

> embedding 维度须与所选 embedding 模型保持一致；更换模型时使用版本化列和双写/回填迁移。

### `entities` 与 `memory_entities`

`entities(id, tenant_id, user_id, entity_type, canonical_name, attributes jsonb, sensitivity, confidence, created_at, updated_at)` 保存联系人、偏好、地点等结构化事实；唯一索引为 `(tenant_id, user_id, entity_type, canonical_name)`。

`memory_entities(memory_id FK, entity_id FK, relation, PRIMARY KEY(memory_id, entity_id))` 连接摘要和实体。

### `provider_connections`

`provider_connections(id, tenant_id, user_id, provider, scopes text[], secret_ref, status, expires_at, created_at, updated_at)`。`secret_ref` 指向 KMS/Secret Manager，**不得**保存 OAuth access token 明文。唯一索引 `(tenant_id, user_id, provider)`。

### `agent_runs`、`tool_calls`、`audit_events`

| 表 | 关键字段 | 说明 |
|---|---|---|
| agent_runs | id, conversation_id, request_id, trace_id, route, status, model_name, token_in/out, estimated_cost, latency_ms | 请求级执行汇总 |
| tool_calls | id, run_id, tool_name, tool_version, action, input_redacted, output_redacted, status, latency_ms, error_code, idempotency_key | 外部调用记录；唯一 `(tool_name, idempotency_key)` |
| audit_events | id, tenant_id, actor_type/id, run_id, action, resource_type/id, decision, reason, metadata_redacted, occurred_at | 只追加；用于合规追溯 |

`audit_events` 按月分区并设置只读角色；保留期由法规与组织策略配置。`agent_runs(trace_id)`、`tool_calls(run_id)` 和 `audit_events(tenant_id, occurred_at DESC)` 必须建立索引。

## 4. 记忆写入、检索与衰减

1. 会话结束或达到阈值后，异步生成摘要和候选实体；安全策略先于写库执行。
2. 检索时先以 `tenant_id + user_id + deleted_at IS NULL + 权限标签` 过滤，再取向量 Top-K，最后按 `similarity × importance × recency × confidence` 重排。
3. 每次命中增加 `access_count` 和 `last_accessed_at`；后台任务定期降低长期未访问、低重要性记忆的分数，达到阈值后归档或删除。
4. 用户删除触发软删除、向量索引清理与审计事件；灾备恢复不应重新暴露已删除数据。
