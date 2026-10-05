# Aegis Calendar MCP Server

这是仅供 Aegis 主后端调用的无用户状态日历工具服务。它不负责用户登录、租户、审批、审计、数据库或日历凭据管理；这些职责全部属于 Aegis 主后端。

```text
Aegis 主后端（认证、权限、审计、连接管理）
  ↓ MCP Streamable HTTP / stdio
Calendar MCP Server（工具与日历业务）
  ↓
Mock / Google / Outlook Provider
```

## 目录

```text
Calendar_mcp_server/
├── pyproject.toml
├── Dockerfile
├── README.md
├── src/aegis_calendar_mcp/
│   ├── __main__.py       # stdio 或 Streamable HTTP 启动入口
│   ├── server.py         # 创建 MCPServer、注册工具
│   ├── config.py         # 进程配置
│   ├── tools/            # MCP 工具定义
│   ├── services/         # CalendarService 领域逻辑
│   ├── providers/        # Mock 与未来的日历平台适配器
│   ├── schemas/          # 请求、输出与日历模型
│   └── observability/    # 最小日志配置
└── tests/integration/
```

调用链：`MCPServer → tools → CalendarService → CalendarProvider`。

## 安装

```powershell
cd D:\AI\Aegis_Agent_main\Calendar_mcp_server
D:\anaconda\envs\Aegis\python.exe -m pip install -e ".[dev]"
```

## 启动

默认以 Streamable HTTP 启动，端点为 `http://127.0.0.1:9001/mcp`：

```powershell
D:\anaconda\envs\Aegis\python.exe -m aegis_calendar_mcp
```

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CALENDAR_PROVIDER` | `mock` | 当前一期只支持 Mock Provider。 |
| `CALENDAR_MCP_TRANSPORT` | `streamable-http` | `streamable-http` 或 `stdio`。 |
| `CALENDAR_MCP_HOST` | `127.0.0.1` | HTTP 监听地址。 |
| `CALENDAR_MCP_PORT` | `9001` | HTTP 监听端口。 |
| `CALENDAR_MCP_PATH` | `/mcp` | MCP HTTP 路径。 |
| `CALENDAR_MCP_ALLOWED_HOSTS` | 本机地址 | MCP SDK 校验的 Host 白名单；容器中加入实际服务域名。 |

stdio 模式：

```powershell
$env:CALENDAR_MCP_TRANSPORT = "stdio"
D:\anaconda\envs\Aegis\python.exe -m aegis_calendar_mcp
```

## 与主后端联调

在 `Aegis_backend/.env` 配置：

```dotenv
CALENDAR_MCP_URL=http://127.0.0.1:9001/mcp
CALENDAR_MCP_TIMEOUT_SECONDS=20
```

主后端负责身份、权限、日历连接和审计，并在工具调用 `_meta` 中传递受控标识。当前服务只校验其格式，不自行认证或持久化用户数据。

## 测试

```powershell
D:\anaconda\envs\Aegis\python.exe -m pytest -q
```

## 当前边界

- 工具：`calendar.list_events`、`calendar.find_free_time`；
- Provider：仅固定数据的 Mock 实现；
- 未提供 `/health`、OpenAPI、管理接口和服务间 API Key；
- 独立部署时，应通过私有网络、服务网格或网关限制仅由主后端访问。
