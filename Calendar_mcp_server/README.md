# Aegis Calendar MCP Server（开发期 Mock）

该服务是 Aegis Worker 的 Calendar MCP Client 所调用的独立 HTTP 服务。当前使用固定 Mock 日程数据，只支持读取，不连接 Google Calendar 或 Outlook。

## 启动

```powershell
cd D:\AI\Aegis_Agent_main\Calendar_mcp_server
D:\anaconda\envs\Aegis\python.exe -m pip install -e ".[dev]"
D:\anaconda\envs\Aegis\python.exe -m uvicorn app.main:app --reload --port 9001
```

健康检查：`http://127.0.0.1:9001/health`。

## 与 Aegis 后端联调

在 `Aegis_backend/.env` 配置：

```dotenv
CALENDAR_MCP_URL=http://127.0.0.1:9001/mcp
CALENDAR_MCP_TIMEOUT_SECONDS=20
```

若本服务配置 `CALENDAR_MCP_API_KEY`，Aegis 后端也必须配置相同的 `CALENDAR_MCP_API_KEY`。

然后执行 Aegis 的开发期连接脚本：

```powershell
psql -h 127.0.0.1 -U aegis_app -d aegis_pa -f ..\Aegis_backend\scripts\003_seed_mock_calendar_connection.sql
```

服务不直接接收浏览器请求。Aegis Worker 将以 MCP JSON-RPC `tools/call` 调用：

- `calendar.list_events`
- `calendar.find_free_time`

Mock 日程固定在 2026-10-05 和 2026-10-06 的 UTC 时间。使用“查看我明天的日程”时，需按该日期或由 Agent 时间解析范围覆盖该日期进行联调。

## 当前限制

- `_meta` 中的 Aegis ID 仅校验 UUID 格式，不访问 Aegis 数据库；
- 不实现 OAuth、凭据存储、Google/Outlook API；
- 不支持创建、修改或删除日历事件。
