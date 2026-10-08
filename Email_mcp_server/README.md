# Aegis Email MCP Server

这是仅供 Aegis 主后端调用的无用户状态、只读邮件工具服务。它不负责用户登录、租户、审批、审计、数据库或邮箱 OAuth 凭据管理；这些职责全部属于 Aegis 主后端。

```text
Aegis 主后端（认证、权限、审计、连接管理）
  ↓ MCP Streamable HTTP / stdio
Email MCP Server（只读邮件工具与邮件业务）
  ↓
Mock / Gmail / Outlook Provider
```

调用链为：`MCPServer → tools → EmailService → EmailProvider`。

## 一期工具

| MCP 工具 | 用途 | 返回范围 |
| --- | --- | --- |
| `mail.messages.list` | 读取当前连接邮箱的全部邮件列表快照 | 发件人、主题、时间、预览、版本、附件数量；不返回正文。 |
| `mail.messages.get` | 读取指定邮件 | 邮件正文及附件元数据；供主后端后续进行摘要、待办提取和回复起草。 |

服务本身不提供写邮件、删邮件或修改邮件的工具。后续的邮件发送必须由主后端完成风险判断与逐项审批后，再单独设计受控写工具。

## 目录

```text
Email_mcp_server/
├── data/mock_mailbox.json       # 固定 Mock 邮箱数据
├── src/aegis_email_mcp/
│   ├── __main__.py              # stdio 或 Streamable HTTP 启动入口
│   ├── server.py                # 创建 MCPServer、注册工具
│   ├── config.py                # 进程配置
│   ├── tools/                   # MCP 工具定义
│   ├── services/                # EmailService 领域逻辑
│   ├── providers/               # Mock 与未来邮件平台适配器
│   ├── schemas/                 # 请求、输出与邮件模型
│   └── observability/           # 最小日志配置
└── tests/integration/
```

## 安装与启动

```powershell
cd D:\AI\Aegis_Agent_main\Email_mcp_server
D:\anaconda\envs\Aegis\python.exe -m pip install -e ".[dev]"
D:\anaconda\envs\Aegis\python.exe -m aegis_email_mcp
```

默认以 Streamable HTTP 启动，端点是 `http://127.0.0.1:9002/mcp`。

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `EMAIL_PROVIDER` | `mock` | 一期只支持本地 JSON Mock Provider。 |
| `EMAIL_MCP_TRANSPORT` | `streamable-http` | `streamable-http` 或 `stdio`。 |
| `EMAIL_MCP_HOST` | `127.0.0.1` | HTTP 监听地址。 |
| `EMAIL_MCP_PORT` | `9002` | HTTP 监听端口。 |
| `EMAIL_MCP_PATH` | `/mcp` | MCP HTTP 路径。 |
| `EMAIL_MCP_ALLOWED_HOSTS` | 本机地址 | MCP SDK 校验的 Host 白名单。 |
| `EMAIL_MOCK_DATA_PATH` | `data/mock_mailbox.json` | 固定 Mock 邮箱数据路径。 |

stdio 模式：

```powershell
$env:EMAIL_MCP_TRANSPORT = "stdio"
D:\anaconda\envs\Aegis\python.exe -m aegis_email_mcp
```

## 与主后端联调

后续接入 Aegis 主后端时，在 `Aegis_backend/.env` 添加：

```dotenv
EMAIL_MCP_URL=http://127.0.0.1:9002/mcp
EMAIL_MCP_TIMEOUT_SECONDS=20
```

主后端在 MCP 调用 `_meta` 中传递 `aegis_connection_id`、`aegis_tenant_id`、`aegis_user_id` 与 `aegis_run_id`。本服务仅校验其格式；Mock Provider 不连接业务数据库，也不自行认证。生产环境应以私有网络、服务网格或网关限制仅允许主后端调用。

## 测试

```powershell
cd D:\AI\Aegis_Agent_main\Email_mcp_server
D:\anaconda\envs\Aegis\python.exe -m pytest -q
```
