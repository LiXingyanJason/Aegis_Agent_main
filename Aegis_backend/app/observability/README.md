# OpenTelemetry 与结构化日志使用说明

本目录提供 Aegis PA 的最小可观测性基础：OpenTelemetry Trace、Redis 队列的跨进程 Trace Context 传播，以及不记录敏感正文的 JSON 结构化日志。

当前目标是让开发人员能依据 `run_id`、`trace_id` 和 `tool_call_id` 排查一次 Agent 任务；暂未部署 Prometheus、Grafana、Loki、Jaeger、Tempo 或 Alertmanager。

结构化日志：记录“发生了什么”。例如 Worker 收到哪个 run_id、日历工具是否成功、耗时多少、错误码是什么。它特别适合排查错误、筛选失败任务和审计执行过程。

OpenTelemetry：记录“这一次请求经过了哪些服务、每一步花了多久”。例如一次用户消息从 API 入队、Worker 消费、调用 LLM、调用 Calendar MCP 的完整链路；即使它跨了不同进程，也能通过同一个 trace_id 串起来。
## 文件职责

```text
observability/
├── telemetry.py            # Trace Provider 与 FastAPI/HTTPX/SQLAlchemy/Redis 自动埋点
├── trace_context.py         # Redis Agent 队列 W3C traceparent 编解码
├── structured_logging.py    # JSON Formatter、日志字段白名单与 trace_id 获取
└── README.md                # 本说明
```

## 已覆盖的任务链路

```text
前端 HTTP 请求
    ↓
FastAPI Span
    ↓
POST /conversations/{conversation_id}/messages
    ↓（数据库事务提交后）
Redis RPUSH：run_id + traceparent
    ↓
Agent Worker BLPOP
    ↓（恢复 traceparent）
agent.consume Span
    ├─ SQLAlchemy Span：任务领取、步骤、消息、事件写入
    ├─ tool.invoke Span
    │   └─ HTTPX Span：Calendar MCP /mcp
    └─ llm.complete Span
        └─ HTTPX Span：模型 Chat Completions API
```

`agent_runs` 创建时会保留原有的 `request_id`，并写入当前 Trace 的 `trace_id`。因此既可从数据库任务记录按 `run_id`/`trace_id` 查日志，也可从日志反查任务。

## 自动埋点、手动埋点与跨进程传播

OpenTelemetry 不是在业务完成后再扫描日志，而是在调用发生时插入 Span。项目采用以下三种方式配合：

```text
自动埋点：记录技术调用（HTTP、SQL、Redis）
手动埋点：记录业务动作（消费任务、调用工具、调用模型）
上下文传播：让 API 与 Worker 的 Span 属于同一个 Trace
```

### 自动埋点

自动埋点相当于为框架和通用库安装监控插件。启动时项目会执行：

```python
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()
RedisInstrumentor().instrument()
SQLAlchemyInstrumentor().instrument(engine=database.engine.sync_engine)
```

此后无需在每个接口、每条 SQL 或每次 Redis 调用旁增加监控代码：

- FastAPI 收到 HTTP 请求时自动创建服务端 Span；
- HTTPX 调用 LLM 或 Calendar MCP 时自动创建出站 HTTP Span；
- SQLAlchemy 执行 SQL 时自动创建数据库 Span；
- Redis 执行 `RPUSH`、`BLPOP`、Pub/Sub 等命令时自动创建 Redis Span。

自动埋点主要回答“调用了什么技术组件、耗时多久、是否发生异常”。

### 手动埋点

手动埋点是在一个具有明确业务含义的操作前后增加少量代码。以工具调用为例：

```python
from app.observability.telemetry import get_tracer

tracer = get_tracer(__name__)
with tracer.start_as_current_span("tool.invoke") as span:
    span.set_attribute("aegis.run_id", str(run_id))
    span.set_attribute("aegis.tool.name", definition.name)
    result = await self._tools.invoke(prepared)
```

进入 `with` 时创建 Span，离开 `with` 时自动结束 Span 并计算耗时；若其中抛出异常，Span 会记录异常状态。当前已手动定义：

| Span 名称 | 业务含义 |
| --- | --- |
| `agent.consume` | Worker 取到队列任务后的完整消费过程 |
| `llm.complete` | Agent 调用模型生成回复 |
| `tool.invoke` | Agent 调用受控工具，例如日历 MCP 工具 |

手动埋点主要回答“这项业务动作是什么、它为何慢或失败”。不要在 Span attribute 中写入用户正文、Token、API Key 或第三方凭据。

### 跨进程上下文传播

API 与 Worker 是不同进程，Worker 不能天然知道它正在继续哪一次 HTTP 请求。因此 API 将 W3C `traceparent` 随 `run_id` 写入 Redis；Worker 取出后恢复该上下文，再创建 `agent.consume` Span。

结果是：API 请求、Redis 投递、Worker、LLM 和 Calendar MCP 调用共享同一个 `trace_id`，而不是多条互不关联的 Trace。

## 配置

配置由 `app.config.settings.Settings` 统一读取，环境变量示例见项目根目录 `.env.example`：

```dotenv
OTEL_ENABLED=true
OTEL_SERVICE_NAME=aegis-pa-api
OTEL_CONSOLE_EXPORTER=false
```

| 配置 | 含义 | 本地建议 |
| --- | --- | --- |
| `OTEL_ENABLED` | 是否初始化 OpenTelemetry SDK | `true` |
| `OTEL_SERVICE_NAME` | API 服务的 OpenTelemetry 服务名 | `aegis-pa-api` |
| `OTEL_CONSOLE_EXPORTER` | 是否将完整 Span 输出到控制台 | 日常开发 `false`，排查链路时临时设为 `true` |

Worker 自动使用 `<OTEL_SERVICE_NAME>-worker` 作为服务名，例如 `aegis-pa-api-worker`。

修改配置后，必须分别重启 API 与 Worker：

```powershell
# 终端 1：API
cd D:\AI\Aegis_Agent_main\Aegis_backend
D:\anaconda\envs\Aegis\python.exe -m uvicorn app.main:app --reload --port 8000

# 终端 2：Agent Worker
cd D:\AI\Aegis_Agent_main\Aegis_backend
D:\anaconda\envs\Aegis\python.exe -m app.worker.agent_worker
```

## JSON 日志

API 生命周期与 Worker 会调用 `configure_structured_logging()`，将根日志器输出转换为一行一个 JSON。常见 Worker 日志如下：

```json
{
  "timestamp": "2026-10-05T01:23:45.678901+00:00",
  "level": "INFO",
  "logger": "app.worker.agent_worker",
  "message": "agent_run_finished",
  "event": "agent_run_finished",
  "run_id": "d99ce16b-7970-46b7-8d18-6b953fdfb5fa",
  "status": "completed",
  "duration_ms": 6520,
  "trace_id": "1234567890abcdef1234567890abcdef",
  "span_id": "1234567890abcdef"
}
```

业务代码应使用 `log_event()` 记录可检索事件：

```python
log_event(
    logger,
    logging.INFO,
    "tool_call_completed",
    run_id=run.id,
    tool_call_id=tool_call_id,
    tool_name=definition.name,
    status="succeeded",
    duration_ms=duration_ms,
)
```

允许写入的业务字段严格限定为：

```text
event、run_id、conversation_id、tool_call_id、queue_name、status、error_code、
duration_ms、provider、model_name、tool_name
```

不要直接将用户输入、邮件正文、模型 prompt/response、`Authorization` 头、access token、API Key、OAuth refresh token 或第三方凭据传入日志。

`JsonFormatter` 会自动从当前 OpenTelemetry Span 提取 `trace_id` 与 `span_id`。需要在代码中把当前链路标识写入 `agent_runs` 时，使用：

```python
from app.observability.structured_logging import get_current_trace_id

trace_id = get_current_trace_id()
```

没有有效 Trace 上下文时，该函数返回 `None`；数据库字段允许为空，不能为此伪造标识。

## Redis 队列 Trace 传播

API 不再只向队列写入纯文本 UUID，而是写入以下 JSON：

```json
{
  "run_id": "d99ce16b-7970-46b7-8d18-6b953fdfb5fa",
  "traceparent": "00-<trace-id>-<span-id>-01"
}
```

`RunDispatchService` 在数据库提交成功后调用 `serialize_run_message()`；`agent_worker` 使用 `deserialize_run_message()` 恢复父上下文，并以该上下文创建 `agent.consume` Span。

为平滑发布，`deserialize_run_message()` 仍兼容旧格式：

```text
d99ce16b-7970-46b7-8d18-6b953fdfb5fa
```

旧消息可以继续执行，但没有 API 侧的父 Trace。

## 添加新的可观测业务步骤

对一个需单独排查的耗时操作，使用模块专属 Tracer 创建 Span：

```python
from app.observability.telemetry import get_tracer

tracer = get_tracer(__name__)
with tracer.start_as_current_span("approval.execute") as span:
    span.set_attribute("aegis.run_id", str(run_id))
    # 执行业务逻辑；不要将用户正文、token 等敏感信息作为 Span attribute。
    await execute_approval()
```

对于 HTTPX、SQLAlchemy、Redis 的常规调用，无需手写 Span，自动埋点会创建子 Span。新增外部 HTTP 服务时，仍应通过项目统一的 HTTPX 客户端调用，以保留 Trace Context。

## 本地排查方法

1. 设置 `OTEL_CONSOLE_EXPORTER=true`，重启 API 和 Worker。
2. 发送一条任务消息，并记录接口返回的 `run_id`。
3. 在 Worker JSON 日志中按 `run_id` 查询 `agent_run_received`、`tool_call_completed`、`agent_run_finished`。
4. 需要跨 API 与 Worker 串联时，使用两端日志中的同一个 `trace_id` 查询。
5. 若有 Calendar MCP 调用，检查 `tool_call_id`、`tool_name`、`duration_ms`、`error_code`，再关联 `tool_calls` 与 `run_events` 数据库记录。

## 当前边界

- 当前仅在 Aegis API 与 Agent Worker 中初始化 Trace Provider；Calendar MCP Server 目前由 Aegis Worker 的 HTTPX 出站 Span 表示，后续可在该服务单独接入 FastAPI OpenTelemetry 埋点以获得服务端 Span。
- `OTEL_CONSOLE_EXPORTER=true` 仅适合本地短时排查；生产环境应改用受控 OTLP Exporter，并配置采样、访问权限、保留策略和脱敏规则。
- OpenTelemetry Trace 不是审计事实来源。审批、工具调用、任务状态与用户可见历史仍必须以 PostgreSQL 业务表为准。

