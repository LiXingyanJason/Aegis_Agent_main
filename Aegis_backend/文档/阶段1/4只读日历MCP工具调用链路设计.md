# 只读日历 MCP 工具调用链路设计

本文说明 Aegis PA 一期的只读日历工具如何被 Agent 调用、如何保证调用受控，以及统一 Tool 契约的职责边界。当前已注册两个工具：

- `calendar.list_events`：查询当前用户在指定时间范围内的日程；
- `calendar.find_free_time`：在指定时间范围内查询满足时长要求的空闲时间。

二者均为 `read` 风险等级，只读取日程，不会创建会议、修改日历或向外发送信息。

## 1. 总体调用链路

```text
用户提交消息
    ↓
AgentToolRouter（确定性关键词路由）
    ↓ ToolInvocation
AgentOrchestrator
    ↓
ToolGateway.prepare()
    ├─ ToolRegistry：查询工具是否在白名单
    ├─ 校验风险等级与连接要求
    └─ 查询当前用户有效的 provider_connection
    ↓ PreparedToolInvocation
ToolGateway.invoke()
    ↓
CalendarMCPToolHandler.invoke()
    ↓
CalendarMCPClient.call()（HTTP JSON-RPC：tools/call）
    ↓
Calendar MCP Server
    ↓ ToolResult / ToolError
AgentOrchestrator
    ├─ 持久化 run_steps、tool_calls、工具结果
    ├─ 写入并发布 SSE 工具预览与进度事件
    └─ 将可信工具结果作为上下文交给 LLM 组织回复
```

当前一期的工具选择不是由 LLM 自行决定，而是由 `AgentToolRouter` 根据用户最后一条消息中的日程相关关键词生成调用意图。LLM 仅根据后端提供的可信工具结果生成自然语言回复，不能直接调用 MCP 服务。

## 2. 统一 Tool 契约

统一契约位于 `app/tool/contracts.py`。其目标是让不同类型的工具——例如日历、邮件、飞书或内部 HTTP 服务——采用相同的输入、输出与错误语义，避免 Agent 编排层依赖任一具体服务的实现细节。

### 2.1 ToolDefinition：工具静态定义

```python
@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    version: str
    risk_level: RiskLevel
    mcp_server: str
    description: str
    requires_calendar_connection: bool = False
```

| 字段 | 作用 |
| --- | --- |
| `name` | 工具唯一名称，如 `calendar.list_events`。 |
| `version` | 工具契约版本，用于后续兼容、追溯。 |
| `risk_level` | 风险等级：`read`、`write`、`sensitive`。 |
| `mcp_server` | 对应的 MCP 服务标识，如 `calendar-mcp`。 |
| `description` | 工具功能说明。 |
| `requires_calendar_connection` | 是否要求当前用户已有有效日历连接。 |

`ToolDefinition` 是工具白名单和安全策略的权威来源。未被注册的工具不能通过 Gateway 调用。

### 2.2 ToolInvocation：调用意图

```python
@dataclass(frozen=True, slots=True)
class ToolInvocation:
    tool_name: str
    arguments: dict[str, Any]
```

它表示“Agent 希望调用哪个工具、使用什么业务参数”，但不携带用户、租户或第三方连接身份。例如：

```python
ToolInvocation(
    tool_name="calendar.list_events",
    arguments={
        "start_at": "2026-10-05T00:00:00+08:00",
        "end_at": "2026-10-06T00:00:00+08:00",
        "requested_date": "2026-10-05",
        "timezone": "Asia/Shanghai",
    },
)
```

其中 `requested_date` 和 `timezone` 用于解释“今天”“明天”等相对时间，以及展示本地时间。真实的 MCP 输入以 `start_at`、`end_at` 等时间参数为准。

### 2.3 ToolContext：可信执行上下文

```python
@dataclass(frozen=True, slots=True)
class ToolContext:
    tenant_id: UUID
    user_id: UUID
    run_id: UUID
    connection_id: UUID | None = None
```

`ToolContext` 只能由后端在完成认证、租户隔离和连接查询后创建；浏览器与 LLM 均不能自行构造。它将当前用户、租户、任务和已验证的第三方连接关联到同一次工具调用。

Calendar MCP Client 会将这些内部标识作为 MCP JSON-RPC 请求的 `_meta` 发送给受信任的日历服务，便于服务端鉴权、审计和任务追溯。

### 2.4 ToolHandler：具体工具实现接口

```python
class ToolHandler(Protocol):
    async def invoke(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        ...
```

所有具体工具都必须遵守该异步接口。当前实现为 `CalendarMCPToolHandler`；未来的邮件或飞书工具可以使用各自的协议实现相同接口，而不用修改 `AgentOrchestrator` 的主流程。

### 2.5 ToolResult：成功结果

```python
@dataclass(frozen=True, slots=True)
class ToolResult:
    response_payload: dict[str, Any]
    output_summary: dict[str, Any]
```

| 字段 | 用途 |
| --- | --- |
| `response_payload` | 工具返回的完整结构化结果，保存到 `tool_calls`，用于审计与后续追溯。 |
| `output_summary` | 经过长度、层级控制的展示摘要，用于 SSE `tool_preview`、前端预览及 LLM 上下文。 |

日历客户端会在摘要中保留原始 ISO 时间，并在指定时区有效时补充 `start_at_local`、`end_at_local` 与 `display_timezone`，使前端和 LLM 可直接展示本地时间。

### 2.6 ToolError：受控失败

```python
class ToolError(RuntimeError):
    code: str
    message: str
```

常见错误码包括：

| 错误码 | 含义 |
| --- | --- |
| `TOOL_UNAVAILABLE` | 工具服务未配置或暂时不可用。 |
| `CALENDAR_CONNECTION_REQUIRED` | 当前用户没有有效日历连接。 |
| `TOOL_EXECUTION_FAILED` | MCP 服务已接收请求但执行失败。 |
| `TOOL_INVALID_RESPONSE` | MCP 服务返回的数据不符合契约。 |

编排层捕获 `ToolError` 后会将 `tool_calls`、`run_steps` 标记为失败，写入 SSE 进度事件；对于只读日历工具，Agent 可继续生成“暂时无法查询”的说明，但不得编造日程数据。

## 3. 注册与执行时序

工具 Handler 在 Worker 启动时创建和注册一次，而不是每次处理任务时临时创建：

```text
Worker 启动
    ↓
create_default_tool_gateway(settings)
    ↓
创建 CalendarMCPToolHandler
    ↓
向 ToolRegistry 注册：ToolDefinition + ToolHandler
    ↓
创建 ToolGateway 并注入 AgentOrchestrator
```

当一次任务命中日历意图时：

```text
AgentToolRouter 生成 ToolInvocation
    ↓
ToolGateway.prepare(...)
    ├─ 查询 Registry 中的 ToolDefinition
    ├─ 校验该工具已注册、允许调用
    ├─ 查询 active 的 provider_connection
    └─ 生成包含 ToolContext 的 PreparedToolInvocation
    ↓
ToolGateway.invoke(prepared)
    ↓
按工具名从 Registry 取出已注册 ToolHandler
    ↓
handler.invoke(context, arguments)
```

这意味着 Handler 不会被“注入给某一次 `Gateway.invoke()`”。Gateway 启动时已持有 Registry，`invoke()` 仅按已验证的工具名查找并执行对应 Handler。

## 4. 使用的设计模式

该设计是多个模式的组合，而不是单一模式。

### 4.1 注册表模式（Registry Pattern）

`ToolRegistry` 维护工具名到定义和实现的映射：

```text
calendar.list_events
    → ToolDefinition + CalendarMCPToolHandler

calendar.find_free_time
    → ToolDefinition + CalendarMCPToolHandler
```

它提供工具白名单。即使上层产生了 `mail.send` 等未注册工具名，Gateway 也会拒绝执行。

### 4.2 策略模式（Strategy Pattern）

不同工具具有不同执行策略，但都实现同一个 `ToolHandler.invoke(context, arguments)` 接口。

```text
ToolGateway
    ↓ 统一 invoke
CalendarMCPToolHandler / MailMCPToolHandler / FeishuToolHandler
```

新增工具时可新增 Handler 实现并注册，无需让 Agent 编排层直接依赖具体服务。

### 4.3 门面模式（Facade Pattern）

`ToolGateway` 向 `AgentOrchestrator` 暴露简化入口：

```python
prepare(...)
invoke(...)
```

它隐藏了 Registry 查询、连接查询、受控上下文构造、Handler 分发等细节，使编排层专注于任务步骤、审计记录和事件发布。

### 4.4 依赖注入（Dependency Injection）

Worker 启动时组装依赖：

```python
tools = create_default_tool_gateway(settings)
orchestrator = AgentOrchestrator(..., tools=tools)
```

`AgentOrchestrator` 不自行创建 Gateway。这使单元测试可以注入 Fake Gateway，也使后续替换或扩展具体工具实现更容易。

## 5. 数据与事件留痕

一次成功的日历查询至少会产生：

```text
run_steps
    创建“正在查询日程/可用时间”步骤，最终标记 succeeded 或 failed

tool_calls
    保存工具名、版本、风险等级、连接、请求参数、完整响应、展示摘要和耗时

run_events
    progress_updated（开始、结束）
    tool_preview（工具安全摘要）
    connection_required（仅缺少连接时）
```

事件先在同一数据库事务中持久化，提交后再经 Redis Pub/Sub 发布，由现有 SSE 接口转发给前端。这样实时推送短暂失败时，客户端仍能根据 `run_events` 补发进度与工具预览。
