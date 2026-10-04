# 只读日历 MCP 工具调用链路设计

## 1. 范围

本文件说明一期已实现的只读日历能力：Agent 根据用户任务选择工具，调用 Calendar MCP Server 查询当前用户日程或可用时间，保存工具调用审计记录，并以 SSE 推送进度与只读预览。

本阶段不创建、修改或删除日历事件；不创建日历草稿，也不进入确认流程。

## 2. 完整链路

```text
用户提交“查看我明天的日程”或“明天约半小时会议，看看可用时间”
    ↓
POST /conversations/{conversation_id}/messages 创建 run_id
    ↓
Agent Worker 领取任务
    ↓
AgentToolRouter 依据最新用户消息选择只读日历工具
    ↓
Tool Registry 白名单校验 → Tool Gateway 校验 read 风险并解析日历连接
    ↓
Calendar MCP Client 通过 HTTP JSON-RPC 调用 Calendar MCP Server
    ↓
Calendar MCP Server 适配 Google Calendar / Outlook Calendar
    ↓
PostgreSQL 保存 run_steps、tool_calls、run_events
    ↓
Redis Pub/Sub 通知 SSE API → 前端展示进度和工具预览
    ↓
LLM 基于可信工具摘要生成最终文字回复
```

## 3. 统一 Tool 契约

定义位于 `app/tool/contracts.py`：

| 对象 | 作用 |
|---|---|
| `ToolDefinition` | 工具名称、版本、风险、MCP Server、连接要求 |
| `ToolInvocation` | Agent 选择出的工具名与结构化参数 |
| `ToolContext` | 后端生成的租户、用户、任务和连接上下文 |
| `ToolResult` | 原始响应与可安全展示的摘要 |
| `ToolError` | 受控错误码及脱敏错误信息 |

浏览器不能提交 `ToolContext` 或任意指定工具。只有已注册、且网关允许的工具才能被调用。

## 4. 已注册的日历工具

| 工具 | 参数 | 风险 | 作用 |
|---|---|---|---|
| `calendar.list_events` | `start_at`、`end_at` | `read` | 查询当前用户日程 |
| `calendar.find_free_time` | `start_at`、`end_at`、`duration_minutes`、`participants[]` | `read` | 查询当前用户可用时间 |

Worker 通过 `app/tool/bootstrap.py` 的 `create_default_tool_gateway()` 注册这两个工具。`ToolRegistry` 是工具白名单；`ToolGateway` 会确认工具已注册、风险为 `read`，并按 `tenant_id + user_id` 查找有效 Google/Outlook 日历连接。

没有连接时，网关返回 `CALENDAR_CONNECTION_REQUIRED`，不会请求外部服务、不会虚构结果。

## 5. Agent 选择规则

`app/agent/tool_router.py` 是一期确定性路由，避免 LLM 任意拼接工具调用：

- “日程”“日历”“行程”等查看语义：`calendar.list_events`；
- “空闲”“可用时间”“会议”“约”“安排”等语义：`calendar.find_free_time`；
- “今天”“明天”转换为 UTC 查询范围；未给日期默认未来 7 天；
- “半小时”或“30 分钟”等转换为时长；默认 30 分钟；
- 邮箱格式参与者写入 `participants[]`。

工具选择将来可以升级为 LLM 规划，但仍必须经过 Registry 和 Gateway。

## 6. Calendar MCP Client

`app/tool/calendar_mcp_client.py` 是 Worker 内的 HTTP 客户端，不是单独启动的 Aegis 服务。它使用 MCP JSON-RPC 的 `tools/call`：

```json
{
  "jsonrpc": "2.0",
  "id": "请求ID",
  "method": "tools/call",
  "params": {
    "name": "calendar.list_events",
    "arguments": {"start_at": "...", "end_at": "..."},
    "_meta": {"aegis_connection_id": "...", "aegis_tenant_id": "...", "aegis_user_id": "...", "aegis_run_id": "..."}
  }
}
```

Calendar MCP Server 应依据受信任的 `connection_id` 获取授权连接；Aegis 不传递 OAuth token 或 `credential_ref`。服务间调用可使用：

```dotenv
CALENDAR_MCP_URL=http://127.0.0.1:9001/mcp
CALENDAR_MCP_API_KEY=服务间密钥
CALENDAR_MCP_TIMEOUT_SECONDS=20
```

未配置 URL 时，普通对话不受影响；日历任务会展示“日历工具服务尚未配置”。

## 7. 数据库与 SSE

每次工具调用写入：

| 表 | 内容 |
|---|---|
| `run_steps` | 查询步骤、起止时间、成功或失败状态 |
| `tool_calls` | 工具定义、连接 ID、完整请求/响应、展示摘要、耗时、错误 |
| `run_events` | 前端实时消费的事件 |

执行顺序：

```text
创建 run_step、tool_call(running)
→ 提交后 SSE：progress_updated(正在查询)
→ 调用 Calendar MCP Server
→ 更新 tool_call、run_step
→ 写入 run_events
→ 提交后 Redis Pub/Sub 通知 SSE API
```

成功时依次发送 `progress_updated`、`tool_preview`、`progress_updated`。`tool_preview` 包含工具名、`read` 风险、输入摘要和只读输出摘要。无连接或调用失败时，写入失败工具记录，推送失败预览和进度；缺少连接时额外推送 `connection_required`。

工具调用完整载荷仅保存于服务端审计表；前端只接收 `input_summary`、`output_summary`，不接收 OAuth token、凭据引用或模型密钥。

## 8. 前端与恢复

前端通过既有 `GET /runs/{run_id}/events` 接收 `tool_preview`、`progress_updated`、`connection_required`，并在右侧运行面板显示工具预览或连接提示。页面刷新时，`GET /conversations/{conversation_id}` 会从 `tool_calls` 恢复相同的安全预览。

## 9. 关键文件

| 文件 | 职责 |
|---|---|
| `app/tool/contracts.py` | 统一工具契约 |
| `app/tool/tool_registry.py` | 白名单注册表 |
| `app/tool/tool_gateway.py` | 风险校验、连接解析、处理器分发 |
| `app/tool/calendar_mcp_client.py` | MCP HTTP JSON-RPC 客户端 |
| `app/tool/bootstrap.py` | Worker 默认日历工具注册 |
| `app/agent/tool_router.py` | 日历意图选择 |
| `app/repository/tool_repository.py` | 连接与工具调用记录 |
| `app/agent/orchestrator.py` | 工具调用、事件与 LLM 上下文注入 |

## 10. 后续范围

- Calendar MCP Server 的部署、OAuth 授权与 Google/Outlook 适配；
- 日历草稿、单项确认后创建事件；
- 时区、工作时间、多参与人可用性高级计算；
- 工具重试、限流、熔断、Transactional Outbox；
- LLM 多步工具规划。
