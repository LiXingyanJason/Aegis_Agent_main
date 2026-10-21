# Aegis Email MCP Server

Mock 数据相对路径现在固定相对于 Email MCP 项目目录，不依赖启动终端或 IDE 的工作目录。默认读取 `Email_mcp_server/data/mock_mailbox.json`；已发送记录写入同目录的 `mock_sent_mailbox.json`。配置 `EMAIL_MOCK_DATA_PATH`、`EMAIL_MOCK_SENT_DATA_PATH` 时，相对路径采用同一规则，绝对路径保持不变。

Docker 明确配置 `/app/data/mock_mailbox.json` 和 `/app/data/mock_sent_mailbox.json`；使用独立安装的包时也应通过环境变量指定数据绝对路径。收件箱文件不存在时仍报错，不自动生成或清空已有邮箱。

这是仅供 Aegis 主后端调用的无用户状态邮件工具服务。它不负责用户登录、租户、审批、审计、数据库或邮箱 OAuth 凭据管理；这些职责全部属于 Aegis 主后端。

```text
Aegis 主后端（认证、权限、审计、连接管理）
  ↓ MCP Streamable HTTP / stdio
Email MCP Server（邮件读取与已批准发送）
  ↓
Mock / Gmail / Outlook Provider
```

调用链为：`MCPServer → tools → EmailService → EmailProvider`。

## 一期工具

| MCP 工具 | 用途 | 返回范围 |
| --- | --- | --- |
| `mail.messages.list` | 读取当前连接邮箱的全部邮件列表快照 | 发件人、主题、时间、预览、版本、附件数量；不返回正文。 |
| `mail.messages.get` | 读取指定邮件 | 邮件正文及附件元数据；供主后端后续进行摘要、待办提取和回复起草。 |
| `mail.messages.send` | 发送已批准草稿 | 外部邮件 ID、发送时间与收件人回执；必须由 Worker 在逐项批准后调用。 |

`mail.messages.send` 不是浏览器 API。主后端必须先冻结草稿、创建 `approval_item`，用户批准后才由 Agent Worker 调用它；调用还必须携带稳定的幂等键。Mock 模式将发送结果写入 `data/mock_sent_mailbox.json`，不会发送真实邮件。

## 目录

```text
Email_mcp_server/
├── data/mock_mailbox.json       # 固定 Mock 邮箱数据
├── data/mock_sent_mailbox.json  # Mock 已发送箱（运行时写入）
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
| `EMAIL_MOCK_SENT_DATA_PATH` | `data/mock_sent_mailbox.json` | Mock 已发送箱的写入路径。 |

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

主后端在 MCP 调用 `_meta` 中传递 `aegis_connection_id`、`aegis_tenant_id`、`aegis_user_id`；由 Agent Worker 发起的摘要、起草等任务还会传递 `aegis_run_id`。本服务仅校验其格式；Mock Provider 不连接业务数据库，也不自行认证。生产环境应以私有网络、服务网格或网关限制仅允许主后端调用。

## 测试

```powershell
cd D:\AI\Aegis_Agent_main\Email_mcp_server
D:\anaconda\envs\Aegis\python.exe -m pytest -q
```

## 阶段8协同：多封匹配与用户选择测试

`data/mock_mailbox.json`保留原3封邮件，新增4封虚构客户的续约会议邮件（共7封，均为example.com示例地址）。不修改原邮件ID/版本，避免旧来源引用失效。

| 完整主题 | 正文会议要求（北京时间） | 预期分支 |
| --- | --- | --- |
| 协同测试A｜星河服务续约会议 | 2026-10-26 09:00～12:00，50分钟，两名明确参会人 | 查询日历并生成待确认草稿 |
| 协同测试B｜云杉平台续约会议 | 2026-10-27 14:00～17:00，30分钟，一名明确参会人 | 不得混用A的日期/时长/参会人 |
| 协同测试C｜青禾系统续约会议 | 2026-10-28 09:00～12:00，未定时长 | 选择邮件后补问时长，例如回答“50分钟” |
| 协同测试D｜远山项目续约会议 | 45分钟，未定会议日期/范围 | 补问，例如回答“北京时间2026-10-29 14:00到17:00”；合同到期日不是会议日期 |

在主对话输入：**“请查找续约会议邮件，按其中的时间要求查询日历并安排会议草稿；如果有多封匹配，请先让我选择，不要默认第一封。”**

实际查询命中多封时，邮件专家通过input_required展示候选并暂停。8.6A起新问题为single_choice，前端需按options展示单选，提交选中的selected_option_id；不能按上表顺序猜“第一封”，也不能向新选择题提交文本主题或序号。旧问题input_type=text时才沿用完整主题/序号回答。Mock按接收时间倒序，新增邮件依次D/C/B/A，真实候选以该次问题快照为准。若模型主动缩小查询到唯一客户，则不会触发多封选择；可换用“协同测试”作为查询线索，且不要在首轮指定某一客户。

8.6A须先迁移主后端021_structured_input_choices，并统一更新API/Worker及单选前端；旧文本表单不能回答新单选，前端未接入时可按根接口文档第25章通过API提交选项ID。本次不修改前端或自动迁移数据库。

也可直接输入“请按《协同测试A｜星河服务续约会议》的要求查询日历并安排会议草稿”测试精确主题链路。草稿仍须用户逐项批准；Mock日期仅为固定测试数据，当前时间晚于这些日期时应另行更新日期/版本，不声称始终可安排或当前用户一定空闲。

需开启Worker的COOPERATION_ENABLED并使用最新版Worker；本次只增加Mock数据，不触发同步、真实发送或创建日历。默认Mock Provider每次list/get都会重新读JSON，使用此文件的Email MCP无需因数据改动重启；若配置了其他EMAIL_MOCK_DATA_PATH请检查实际数据源。已展示旧卡片不会自动刷新，重新发起查询；邮件管理页如用SQL快照，须通过原有同步入口刷新。新增日期的可用性仍以当前日历查询结果为准。
