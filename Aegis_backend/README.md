# Aegis PA 后端

## 本地开发环境

后端使用 Python 3.12、FastAPI、SQLAlchemy Async、PostgreSQL、Redis 和 Keycloak OIDC。

安装项目及开发依赖：

```powershell
cd D:\AI\Aegis_Agent_main\Aegis_backend
D:\anaconda\envs\Aegis\python.exe -m pip install -e ".[dev]"
```

从 `.env.example` 复制一份 `.env`，填写数据库、Redis、OIDC 和模型服务配置。`.env` 不得提交到 Git。

运行基础设施测试：

```powershell
D:\anaconda\envs\Aegis\python.exe -m pytest tests/config -q
```

运行全部后端测试：

```powershell
D:\anaconda\envs\Aegis\python.exe -m pytest tests -q
```

## 当前已实现的会话与任务接口

启动服务后可访问 Swagger：`http://127.0.0.1:8000/docs`。下列接口都要求请求头包含 `Authorization: Bearer <access_token>`；后端验证 Keycloak token 并按本地 `tenant_id + user_id` 限制数据范围。

| 方法 | 当前路径 | 用途 |
|---|---|---|
| `POST` | `/conversations` | 创建当前用户的新会话 |
| `GET` | `/conversations` | 获取当前用户的历史会话摘要 |
| `GET` | `/conversations/{conversation_id}` | 恢复一个会话的可见消息、运行进度、工具预览及确认项 |
| `POST` | `/conversations/{conversation_id}/messages` | 保存用户任务消息，并创建初始状态为 `queued` 的任务运行 |
| `GET` | `/runs/{run_id}` | 查询当前用户任务的状态、模型信息、进度步骤、结果摘要或脱敏错误 |
| `GET` | `/runs/{run_id}/events` | 以 SSE 补发并实时推送当前任务的进度、回复、完成或失败事件 |

当前开发版本尚未挂载 `/api/v1` 前缀。查询其他用户的会话不会暴露其存在性，统一返回 `404`。发送消息时，前端必须提交 `client_message_id`；同一会话内重复提交相同标识会复用原消息和任务，而不会重复创建任务。

## 第一版 Agent Worker 与 LLM 链路

第一版已实现“文本任务 → LLM 文本回复 → SSE 状态/结果通知”。MCP 工具、审批和邮件/日历外部写入尚未接入。任务处理流程如下：

```text
POST /conversations/{conversation_id}/messages
→ PostgreSQL：写入用户消息和 queued agent_run
→ 数据库事务提交后：将 run_id 写入 Redis 列表队列
→ Agent Worker：取出 run_id，将任务更新为 running
→ 读取该会话最近 20 条消息，调用 OpenAI 兼容 Chat Completions 接口
→ PostgreSQL：保存 run_events、assistant 消息、run_steps，并将任务更新为 completed 或 failed
→ Redis Pub/Sub：通知 SSE API 实例向前端推送新增事件
```

除启动 API 外，另开一个终端启动 Worker：

```powershell
cd D:\AI\Aegis_Agent_main\Aegis_backend
D:\anaconda\envs\Aegis\python.exe -m app.worker.agent_worker
```

确保 `.env` 中的 Redis 与模型配置可用：

```dotenv
REDIS_URL=redis://127.0.0.1:6379/0
AGENT_QUEUE_NAME=aegis:agent-runs
MODEL_PROVIDER=openai
MODEL_API_BASE=https://api.openai.com/v1
MODEL_API_KEY=你的模型密钥
MODEL_DEFAULT_NAME=gpt-4.1-mini
```

DeepSeek 使用 OpenAI 兼容 Chat Completions 协议，也可配置为：

```dotenv
MODEL_PROVIDER=deepseek
MODEL_API_BASE=https://api.deepseek.com/v1
MODEL_API_KEY=你的 DeepSeek 密钥
MODEL_DEFAULT_NAME=deepseek-chat
```

如果 Worker 未启动，消息接口仍会成功创建 `queued` 任务，但不会产生 Agent 回复；Worker 启动后会消费后续投递到 Redis 的任务。Redis 中只存储 `run_id`，会话内容和模型结果始终存储在 PostgreSQL。

## SSE 实时任务事件

发送消息成功后，前端可在保留轮询兜底的同时订阅：

```text
GET /runs/{run_id}/events
Authorization: Bearer <access_token>
Last-Event-ID: <最近已处理的 event_no，可选>
```

服务端先按 `event_no` 从 PostgreSQL `run_events` 补发断线期间漏掉的事件，再订阅 Redis Pub/Sub 通道 `aegis:run:{run_id}:events` 接收实时通知。每条事件均已先写入数据库；Redis 通知短暂失败不会丢失事件，下次连接仍可补发。

当前第一版会产生以下事件：

| 事件名 | `data.payload` 关键内容 | 前端行为 |
|---|---|---|
| `run_started` | 状态、当前阶段、模型提供方和模型名 | 将任务标记为运行中 |
| `progress_updated` | 步骤 ID、标签、状态 | 更新右侧进度 |
| `assistant_message_completed` | 最终回复 `content` | 在对话区写入 Agent 完整消息 |
| `run_completed` | 完成状态、结果摘要 | 停止本任务的轮询和 SSE 订阅 |
| `run_failed` | 失败状态、脱敏错误码和提示 | 展示失败提示并停止订阅 |

SSE 帧中的 `id` 等于 `event_no`，前端应保存最近成功处理的编号，并在重连时放入 `Last-Event-ID`。由于浏览器原生 `EventSource` 不能附加 `Authorization` 请求头，当前 Bearer Token 认证方案下应使用 `fetch` 的流式读取（`ReadableStream`）订阅该接口；不要把 access token 拼接到 URL 查询参数。`GET /runs/{run_id}` 仍建议保留为页面刷新、SSE 不可用时的轮询兜底，最终可通过 `GET /conversations/{conversation_id}` 恢复完整会话。

已在旧版本数据库执行过初始化脚本时，还需要执行一次消息幂等约束修复脚本；它只修复 `client_message_id` 的唯一约束，不删除业务数据：

```powershell
psql -h 127.0.0.1 -U aegis_app -d aegis_pa -f scripts/002_fix_message_client_id_unique_constraint.sql
```

## Keycloak OIDC 本地配置

开发环境使用本机 Docker Keycloak 完成登录认证。Aegis 不保存用户密码；Keycloak 签发 access token，后端随后验证 token 的签名、签发者、受众和过期时间。
![img.png](img.png)
### 1. 启动 Keycloak

确保 Docker Desktop 已启动后执行：

```powershell
docker run --name aegis-keycloak `
  -p 127.0.0.1:8080:8080 `
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin `
  -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin `
  quay.io/keycloak/keycloak:26.7.4 start-dev
```

首次执行会下载镜像并创建容器。后续可在 Docker Desktop 的 **Containers** 页面启动 `aegis-keycloak`，或执行：

```powershell
docker start aegis-keycloak
```

打开 `http://127.0.0.1:8080/admin`，使用 `admin/admin` 登录管理控制台。该账户仅限本地开发，不能用于生产环境。

### 2. 创建 Realm 与测试用户

1. 在管理控制台创建 Realm：`aegis`。
2. 在 **Users → Add user** 创建测试用户，例如 `jason`。
3. 为用户设置密码，并将 **Temporary** 关闭；可设置邮箱并标记为已验证。

### 3. 创建前端登录 Client

在 `aegis` Realm 内，进入 **Clients → Create client**，创建：

```text
Client type: OpenID Connect
Client ID: aegis-pa-web
Client authentication: Off
Standard flow: On
```

在登录设置中配置 Vue 开发地址：

```text
Root URL: http://localhost:5173
Valid redirect URIs: http://localhost:5173/*
Web origins: http://localhost:5173
```

### 4. 创建 API Audience Client

再次创建一个 Client：

```text
Client type: OpenID Connect
Client ID: aegis-pa-api
Client authentication: Off
Standard flow: Off
Direct access grants: Off
```

`aegis-pa-api` 不负责网页登录，而是 Aegis 后端 API 的资源标识。

### 5. 配置 Audience Mapper

进入：

```text
Clients → aegis-pa-web → Client scopes → aegis-pa-web-dedicated
→ Configure a new mapper → Audience
```

填写并保存：

```text
Name: aegis-pa-api-audience
Included Client Audience: aegis-pa-api
Add to access token: On
```

在 **Client scopes → Evaluate** 中选择测试用户并点击 **Generate access token**。生成的 access token 的 Payload 必须包含：

```json
{
  "aud": ["aegis-pa-api", "account"]
}
```

`aud` 为字符串或数组均可；后端只要求其包含 `aegis-pa-api`。不要把真实 access token 写入仓库、截图或聊天记录。

### 6. 配置后端环境变量

当前本地 Keycloak 实际签发的 `iss` 为 `http://127.0.0.1:8080/realms/aegis`，因此 `.env` 中必须使用同一地址：

```dotenv
OIDC_ISSUER_URL=http://127.0.0.1:8080/realms/aegis
OIDC_AUDIENCE=aegis-pa-api
OIDC_CLIENT_ID=aegis-pa-web
```

首次使用 Keycloak 测试用户时，用户尚不存在于 Aegis 的 `users` 表。仅在本地开发环境，可额外开启自动映射：

```dotenv
AUTO_PROVISION_USERS=true
DEFAULT_TENANT_NAME=aegis-dev
```

后端会在 `aegis-dev` 租户中创建或复用该 OIDC 用户。生产环境必须保持 `AUTO_PROVISION_USERS=false`，由管理员预先创建用户与租户成员关系，不能让任意外部身份自动加入已有租户。

可访问以下 Discovery 地址确认 Keycloak 正常运行：

```text
http://127.0.0.1:8080/realms/aegis/.well-known/openid-configuration
```

后端令牌验证将使用该配置中的 JWKS 公钥地址，并校验：

- `iss` 必须等于 `OIDC_ISSUER_URL`；
- `aud` 必须包含 `OIDC_AUDIENCE`；
- token 签名有效且未过期；
- 使用 `iss + sub` 查找或创建 Aegis 本地用户。
