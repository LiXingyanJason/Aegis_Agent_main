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
