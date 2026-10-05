# Aegis Calendar MCP Server

这是供 Aegis Agent Worker 调用的独立日历 MCP 服务。当前使用固定 Mock 日程数据，仅支持查询日程和空闲时间；不连接 Google Calendar、Outlook，也不会创建、修改或删除日历事件。

## 前置条件

- Windows PowerShell；
- Python 3.12+，本项目开发环境使用 `D:\anaconda\envs\Aegis\python.exe`；
- 单独启动本服务时，不需要 PostgreSQL、Redis、Keycloak 或 Aegis 主后端。

## 首次安装

```powershell
cd D:\AI\Aegis_Agent_main\Calendar_mcp_server
D:\anaconda\envs\Aegis\python.exe -m pip install -e ".[dev]"
```

`-e` 是可编辑安装：之后修改 `app/` 中的源码，无需重新安装。

## 配置

| 环境变量 | 必填 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `CALENDAR_PROVIDER` | 否 | `mock` | 当前一期仅支持 Mock Provider。 |
| `CALENDAR_MCP_API_KEY` | 否 | 无 | 设置后，Aegis Worker 必须用相同 Bearer Key 调用本服务。 |

本地开发不设置变量也能启动。若要在当前 PowerShell 窗口启用服务间认证：

```powershell
$env:CALENDAR_PROVIDER = "mock"
$env:CALENDAR_MCP_API_KEY = "replace-with-shared-secret"
```

> `.env.example` 是配置参考。当前服务直接读取进程环境变量，因此变量应在启动 Uvicorn 的同一 PowerShell 窗口中设置。

## 启动

在 `Calendar_mcp_server` 目录执行：

```powershell
D:\anaconda\envs\Aegis\python.exe -m uvicorn app.main:app --reload --port 9001
```

成功后终端会显示：

```text
Uvicorn running on http://127.0.0.1:9001
```

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:9001/health` | 健康检查 |
| `http://127.0.0.1:9001/mcp` | MCP JSON-RPC 接口（POST） |
| `http://127.0.0.1:9001/docs` | FastAPI OpenAPI 页面 |

`--reload` 仅用于本地开发，源码变更后会自动重启。生产环境应移除它，并采用容器或进程管理器运行服务。

## 验证启动

另开一个 PowerShell 窗口：

```powershell
Invoke-RestMethod http://127.0.0.1:9001/health
```

预期响应：

```json
{
  "data": {
    "status": "ok",
    "provider": "mock_calendar"
  }
}
```

验证 MCP 工具发现：

```powershell
$body = @{
  jsonrpc = "2.0"
  id = 1
  method = "tools/list"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri http://127.0.0.1:9001/mcp `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

预期能看到：

- `calendar.list_events`
- `calendar.find_free_time`

若启用了 `CALENDAR_MCP_API_KEY`，在请求中额外加入：

```powershell
-Headers @{ Authorization = "Bearer replace-with-shared-secret" }
```

## 与 Aegis 主后端联调

建议启动顺序：

```text
1. 启动 Calendar MCP Server
2. 配置 Aegis_backend/.env
3. 写入 Mock 日历连接
4. 启动 Aegis API 与 Agent Worker
5. 前端发送日历任务
```

### 配置主后端

在 `Aegis_backend/.env` 添加或确认：

```dotenv
CALENDAR_MCP_URL=http://127.0.0.1:9001/mcp
CALENDAR_MCP_TIMEOUT_SECONDS=20
```

若本服务启用了 API Key，主后端也必须配置相同的值：

```dotenv
CALENDAR_MCP_API_KEY=replace-with-shared-secret
```

修改主后端 `.env` 后必须重启 Agent Worker，因为 Worker 会缓存运行时配置。

### 创建开发期日历连接

在 `Aegis_backend` 目录执行：

```powershell
psql -h 127.0.0.1 -U aegis_app -d aegis_pa -f scripts\003_seed_mock_calendar_connection.sql
```

没有有效连接时，Aegis 主后端会返回 `CALENDAR_CONNECTION_REQUIRED`，不会实际请求本服务。

### 启动 Worker 并验证联调

```powershell
cd D:\AI\Aegis_Agent_main\Aegis_backend
D:\anaconda\envs\Aegis\python.exe -m app.worker.agent_worker
```

随后在前端输入：

```text
查看我明天的日程
```

或：

```text
明天和 wangmin@example.com 约半小时会议，看看可用时间
```

Worker 输出以下日志，表示主后端已成功调用本服务：

```text
POST http://127.0.0.1:9001/mcp "HTTP/1.1 200 OK"
```

## Mock 数据

当前固定日程为 UTC 时间：

| 时间 | 日程 |
| --- | --- |
| 2026-10-05 07:00–07:30 | 项目同步 |
| 2026-10-05 09:00–10:00 | 产品评审 |
| 2026-10-06 02:00–03:00 | 周报整理 |

主后端会依据 `APP_TIMEZONE` 解释“今天”“明天”等相对日期，默认是 `Asia/Shanghai`。联调时请让查询范围覆盖以上固定时间。

## 停止

在运行 Uvicorn 的终端按 `Ctrl + C`。

## 常见问题

| 现象 | 处理方式 |
| --- | --- |
| 端口 9001 已占用 | 更换端口，并同步更新主后端 `CALENDAR_MCP_URL`。 |
| `TOOL_UNAVAILABLE` | 检查 `/health`、`CALENDAR_MCP_URL`，并重启 Agent Worker。 |
| `CALENDAR_CONNECTION_REQUIRED` | 执行 `003_seed_mock_calendar_connection.sql`，确认连接状态为 `active`。 |
| MCP 返回 401 | 检查两个服务的 `CALENDAR_MCP_API_KEY` 是否完全一致。 |
| 查不到日程 | Mock 数据固定，检查查询范围是否覆盖 2026-10-05 或 2026-10-06。 |

## 当前目录职责

```text
app/
├── controller/     # HTTP / JSON-RPC 接口
├── service/        # MCP 分发、日历业务逻辑
├── provider/       # Mock / Google / Outlook Provider Adapter
├── param/          # 输入参数校验
├── entity/         # 日历领域对象
├── vo/             # MCP 输出模型
├── security/       # API Key、Aegis _meta 校验
├── common/         # MCP 响应、异常、常量
├── config/         # 配置与依赖组装
└── main.py         # 应用启动入口
```

调用方向：`Controller → MCPService → CalendarService → CalendarProvider`。

## 当前限制

- `_meta` 中的 Aegis ID 目前只校验 UUID 格式，不访问 Aegis 主数据库；
- 未实现 OAuth、凭据存储、Google/Outlook API；
- 当前仅允许两个只读工具；
- 真实日历提供商将在 `app/provider/` 下扩展。
