# Aegis PA 数据库迁移使用说明

本目录由 Alembic 管理 Aegis PA 的 PostgreSQL 数据库结构。后续任何表、索引、约束、RLS 策略或函数变更，均应新增一个迁移版本；不要再通过手工 SQL 文件直接修改共享环境数据库。

## 目录说明

```text
migrations/
├── env.py                 # Alembic 异步连接与运行环境，复用 app.config.settings
├── versions/              # 按版本顺序执行的 Python 迁移文件
├── sql/                   # 已固化的历史 SQL 基线，仅供初始迁移调用
├── sql_runner.py          # 执行历史 SQL 基线的辅助工具
└── README.md              # 本说明
```

当前迁移链：

```text
001_initial_schema → 002_msg_client_id_uq（head）
```

`002_msg_client_id_uq` 是实际 Alembic revision ID。不要使用历史 SQL 文件名 `002_fix_message_client_id_unique_constraint` 作为 `stamp` 参数。

## 使用前准备

必须从 `Aegis_backend` 目录执行命令：

```powershell
cd D:\AI\Aegis_Agent_main\Aegis_backend
```

项目根目录还包含独立的 `Calendar_mcp_server/app`。若从仓库根目录运行，Python 可能错误导入另一个服务的 `app` 包。

准备 `.env`，至少配置：

```dotenv
DATABASE_URL=postgresql+asyncpg://数据库用户:数据库密码@127.0.0.1:5432/aegis_pa
```

`env.py` 会读取项目 `Settings`，因此 `.env` 中其他必填配置（Redis、OIDC、模型配置）也必须有效。安装依赖：

```powershell
D:\anaconda\envs\Aegis\python.exe -m pip install -e ".[dev]"
```

## 常用命令

### 查看当前数据库版本

```powershell
D:\anaconda\envs\Aegis\python.exe -m alembic current
```

### 查看代码中的最新版本

```powershell
D:\anaconda\envs\Aegis\python.exe -m alembic heads
```

正常情况下应看到：

```text
002_msg_client_id_uq (head)
```

### 初始化全新空数据库

确认目标数据库为空后执行：

```powershell
D:\anaconda\envs\Aegis\python.exe -m alembic upgrade head
```

该命令按顺序执行全部版本，创建业务表、RLS 策略、函数、触发器及索引。

### 将旧版开发数据库纳入 Alembic 管理

若数据库已由旧脚本 `scripts/001_initial_schema.sql` 与 `scripts/002_fix_message_client_id_unique_constraint.sql` 完整初始化，**不要**执行 `upgrade head`，否则可能重复建表。

先核对数据库结构与当前代码一致，再登记版本：

```powershell
D:\anaconda\envs\Aegis\python.exe -m alembic stamp 002_msg_client_id_uq
```

`stamp` 只更新数据库的 `alembic_version` 记录，不会执行 DDL，也不会改动业务数据。

若此前误登记了已经不存在的 revision ID，可在确认数据库结构确实处于当前 head 后执行：

```powershell
D:\anaconda\envs\Aegis\python.exe -m alembic stamp --purge 002_msg_client_id_uq
```

`--purge` 会清理旧的 Alembic 版本记录后重新登记；它仍不会创建或删除业务表。不要把它当作修复数据库结构的工具。

### 预览升级 SQL

以下命令只生成 SQL 到终端，不连接或修改数据库：

```powershell
D:\anaconda\envs\Aegis\python.exe -m alembic upgrade head --sql
```

发布到共享环境前，应先审阅该输出，尤其是 `DROP`、`ALTER TABLE`、RLS、触发器和分区相关语句。

## 新增迁移的工作流

1. 先更新数据库设计文档，明确表、字段、约束、RLS 与回滚影响。
2. 创建迁移骨架：

   ```powershell
   D:\anaconda\envs\Aegis\python.exe -m alembic revision -m "add calendar drafts"
   ```

3. 在新生成的 `versions/<revision>_add_calendar_drafts.py` 中手工编写 `upgrade()` 与 `downgrade()`。
4. 对涉及租户数据的表、索引和策略，确保 SQL 使用 `tenant_id`，且 RLS 规则遵循现有安全模型。
5. 在本地空数据库执行 `upgrade head`，在测试数据库验证升级和降级。
6. 运行后端测试：

   ```powershell
   D:\anaconda\envs\Aegis\python.exe -m pytest tests -q
   ```

7. 将迁移文件与对应业务代码在同一次提交中提交。

当前项目尚未建立完整 SQLAlchemy ORM `metadata`，因此不使用 `alembic revision --autogenerate`。所有 DDL 必须人工编写和审核。

## 迁移编写要求

- 每个迁移只处理一个可审查的业务目的，例如“新增会议草稿表”；
- 必须同时考虑 `upgrade()` 与 `downgrade()`；若不可安全回滚，应在迁移文件注释与发布说明中明确；
- 禁止修改已经发布的历史迁移文件；修正问题应追加新版本；
- 生产环境不允许用 `stamp` 跳过未知结构变更；
- 不在迁移中写入真实账号、OIDC subject、API Key、邮件正文或其他生产敏感数据；
- 涉及大表时避免长时间锁表，优先采用可分阶段发布的变更方案；
- 业务数据修复应与结构变更分开，并提供可重复执行、可审计的脚本。

## 排查提示

| 现象 | 优先检查 |
| --- | --- |
| `Can't locate revision identified by ...` | 使用 `alembic heads` 核对真实 revision ID；旧库可在确认结构后用 `stamp --purge 002_msg_client_id_uq` 修正版本记录。 |
| 配置校验失败 | 检查当前目录是否为 `Aegis_backend`，并确认 `.env` 的数据库、Redis、OIDC、模型配置完整。 |
| `relation already exists` | 数据库可能已由旧脚本初始化；不要继续重试 `upgrade`，先确认版本并使用 `stamp`。 |
| RLS 导致迁移失败 | 迁移由数据库管理员连接执行，检查该连接是否有 DDL 权限；不要在迁移中复用普通用户请求的租户上下文。 |

