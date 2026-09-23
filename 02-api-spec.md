# Aegis PA 接口文档

## 1. 通用约定

- Base URL：`/api/v1`
- 鉴权：`Authorization: Bearer <access_token>`；令牌携带 `sub`（用户）、`tenant_id`、角色与 OAuth scopes。
- 格式：JSON；时间使用 ISO 8601 UTC；ID 使用 UUID。
- 所有写请求建议携带 `Idempotency-Key`；响应包含 `X-Request-Id`。

### 通用错误体

```json
{"error":{"code":"TOOL_PERMISSION_DENIED","message":"缺少 calendar.write 权限","request_id":"uuid","retryable":false}}
```

常用状态：`400` 参数错误、`401` 未认证、`403` 无权限、`404` 不存在、`409` 幂等冲突、`422` 策略拒绝、`429` 限流、`502` 工具上游失败、`503` 模型或依赖不可用。

## 2. Agent 对话与确认

### `POST /agent/messages`

提交一条用户消息；可通过 SSE 接收节点进度、确认请求和最终文本。

```json
{
  "conversation_id": "uuid（可选，新会话省略）",
  "content": "明天下午和王敏约半小时项目同步，并发邮件确认",
  "stream": true,
  "locale": "zh-CN",
  "timezone": "Asia/Shanghai"
}
```

响应 `202`：

```json
{
  "request_id":"uuid", "conversation_id":"uuid", "run_id":"uuid",
  "status":"running", "stream_url":"/api/v1/agent/runs/uuid/events"
}
```

SSE 事件：`status`、`assistant_delta`、`tool_preview`、`confirmation_required`、`final`、`error`。`tool_preview` 必须包含已脱敏的参数、风险等级及工具版本。

### `GET /agent/runs/{run_id}/events`

以 `text/event-stream` 返回运行状态和文本分片，支持 `Last-Event-ID` 续传。

### `POST /agent/runs/{run_id}/confirmations/{confirmation_id}`

仅确认展示过的、未过期且参数哈希匹配的操作。

```json
{"decision":"approve","confirmation_token":"signed-token"}
```

`decision` 可为 `approve` 或 `reject`。确认成功返回 `202`，流程从断点继续。令牌单次使用、默认 10 分钟有效。

## 3. 会话与记忆

### `GET /conversations/{conversation_id}`

返回已授权会话及分页消息；敏感字段按角色脱敏。

### `GET /memories?query={text}&type={summary|entity}&limit=20`

在当前用户范围内检索长期记忆，返回 `id`、摘要/实体、来源、置信度、更新时间和匹配分数。

### `PATCH /memories/{memory_id}`

更新用户可编辑记忆：

```json
{"content":"用户偏好周一上午不安排会议", "is_sensitive":false}
```

### `DELETE /memories/{memory_id}`

软删除记忆并发出向量删除任务；删除操作写入审计日志。

## 4. 工具与连接

### `GET /tools`

返回当前用户可用工具及其 `name`、`version`、`risk_level`、所需 scope、输入 JSON Schema。不会暴露凭据或内部 MCP 地址。

### `POST /connections/{provider}/authorize`

创建 OAuth 授权流程。`provider`：`calendar` 或 `email`。返回授权 URL 与 `state`；回调处理后仅保存加密 token 引用。

## 5. 管理与观测接口

### `GET /admin/audit-events?from=&to=&actor_id=&action=`

管理员查询已脱敏审计事件。需 `audit.read` scope，且分页最大 100。

### `GET /metrics`

Prometheus 抓取端点，仅暴露于集群内部；不含用户文本、邮箱地址、令牌或原始提示词。
