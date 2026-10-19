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
001_initial_schema → 002_msg_client_id_uq → 003_mail_background_runs → 004_todo_plan_management（head）
```

`002_msg_client_id_uq` 是邮件消息唯一约束修订的实际 Alembic revision ID；当前 head 为 `004_todo_plan_management`。不要使用历史 SQL 文件名 `002_fix_message_client_id_unique_constraint` 作为 `stamp` 参数。

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
004_todo_plan_management (head)
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

随后必须执行一次升级，以创建邮件后台任务请求表、待办计划字段、受确认控制的 `mail_send` 任务及记忆来源任务字段：

```powershell
D:\anaconda\envs\Aegis\python.exe -m alembic upgrade head
```

若此前误登记了已经不存在的 revision ID，可在确认数据库结构确实处于当前 head 后执行：

```powershell
D:\anaconda\envs\Aegis\python.exe -m alembic stamp --purge 006_memory_source_run
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

### 015：共享索引绑定与软删除策略

当前head为`016_knowledge_category_default`。先备份并停止旧API／Worker，再执行`python -m alembic upgrade head`，一起更新主后端、Agent Worker及Index Worker。

016仅为停用的知识资料类别列添加兼容默认值，不改写历史类别数据；新版上传接口不再接收类别、列表与详情不再返回类别，检索不按类别过滤。必须先迁移再部署新版API。
版本增加vector_namespace／vector_collection，从已有索引任务回填，不创建新索引或云端资源。
共享检索从已授权版本读取绑定，不再依赖仅所有者可见的私人索引任务。
RLS允许所有者处理自身已删除文档的元数据以完成软删除；应用查询仍显式排除deleted_at，
内容子表的SELECT与ALL策略、索引任务策略同时排除删除资料，不能只修改文档策略。
无跨租户或共享者管理授权放宽，不取消FORCE RLS。降级前停新版服务，会移除版本绑定列并恢复旧策略；不删资料或云向量。
本轮仅临时schema验证并回滚，未升级现有业务库。详见[6.6说明](../../aegis_agent_worker/文档/阶段1/15-知识资料接口-SSE与来源卡片-阶段6.6.md)。

### 013：上传、索引与启用分离

本阶段revision为`013_knowledge_lifecycle`，当前已追加至015。先备份、停止旧查询代码、执行`python -m alembic upgrade head`，再部署新版API／Worker。
上传幂等凭据迁至文档；未索引版本允许模型空间为空。文档新增enabled，旧资料与新资料均默认false。
已完成索引不会重建，已有排队任务不会取消；ready资料须所有者显式启用后才可交付证据。
013降级遇到模型空间为空的上传资料会拒绝；降级会移除启用标记及新上传幂等字段，须备份并停止查询，不能stamp绕过。
迁移只在可回滚临时schema验证，未在现有业务库执行。

### 012：知识导入与索引

`012_knowledge_indexing`不是已移除的pgvector迁移；当前head已追加为013。
执行`python -m alembic upgrade head`后才部署知识导入/Index Worker；不会安装pgvector。
新导入源文件保存在受控本机目录，版本表加入文件引用并兼容旧source_text。
同时加入幂等请求、SQL租约恢复、批次进度及向量空间绑定，不实现多版本更新。
有外置源文件时降级明确拒绝，先备份并恢复内联文本；不要stamp绕过。
本次只在临时schema验证012，未升级现有业务库。详见[6.3说明](../../aegis_agent_worker/文档/阶段1/12-知识资料导入与索引-阶段6.3.md)。

### 010～011：知识库及Embedding（Qdrant Cloud路线）

010新增四张知识表及私有/同租户共享RLS；011新增独立Embedding预算与审计。
本地库已升级到011。向量索引改用Qdrant Cloud，未发布且未执行的012 pgvector迁移已移除；
当时head为011，现已追加012／013，不回滚010/011，也不使用stamp。后续SQL变更继续追加新迁移。
生产运行角色须非超级用户且无BYPASSRLS。Qdrant配置不会自动索引或开启知识检索。
降级会删除预算/审计及知识数据，先停相关代码并备份；SQL迁移不删除远端collection。
配置与部署步骤见[知识库权限与Embedding治理](../../aegis_agent_worker/文档/阶段1/11-知识库权限与Embedding治理-阶段6.2.md)。

### 009：模型用量审计

`009_model_usage_audit` 为 `run_execution_attempts` 增加用途、模型信息和三个可空 Token 列。
先执行 `python -m alembic upgrade head`，再启动新版 Worker；旧 API／Worker 兼容新增可空列。
缺失用量和历史数据保持 NULL，不回填 0。迁移不改变 RLS、字符预算或任务状态。
降级前停止新版 Worker 并备份审计数据，降级会删除新增列。
详见 [路由审计与模型用量可观测性](../../aegis_agent_worker/文档/阶段1/08-路由审计与模型用量可观测性.md)。

| 现象 | 优先检查 |
| --- | --- |
| `Can't locate revision identified by ...` | 使用 `alembic heads` 核对真实 revision ID；旧库可在确认结构后用 `stamp --purge 002_msg_client_id_uq` 修正版本记录。 |
| 配置校验失败 | 检查当前目录是否为 `Aegis_backend`，并确认 `.env` 的数据库、Redis、OIDC、模型配置完整。 |
| `relation already exists` | 数据库可能已由旧脚本初始化；不要继续重试 `upgrade`，先确认版本并使用 `stamp`。 |
| RLS 导致迁移失败 | 迁移由数据库管理员连接执行，检查该连接是否有 DDL 权限；不要在迁移中复用普通用户请求的租户上下文。 |

### 017：手动长期记忆生命周期

先停旧API写入与旧Agent Worker并备份，再执行`python -m alembic upgrade 017_memory_manual_lifecycle`，
然后一起更新API、Worker与前端。旧Worker仅按is_sensitive读取，不能与新pending记录混跑。
旧手动有效记录回填confirmed/确认时间、旧敏感布尔显式映射；不删除原数据或重建向量。
新增memory_item_revisions审计（无正文副本）及实体部分唯一索引，来源函数为SECURITY INVOKER。
运行角色需要新表写/读权限及来源事实读取权限，保持RLS，不将应用账户提升为超级用户。
downgrade会丢弃新增字段/审计，存在无法用旧格式表达的有效数据时主动拒绝；必须先人工导出处理。
实现测试只在随机临时schema/受限角色内回滚运行，未升级业务数据库。

### 018：按模型用途的记忆审计

停止Agent Worker与API写入并备份，执行 `python -m alembic upgrade 018_memory_call_usage` 后再启动新版服务。
新增run_memory_usages的attempt/run/tenant复合外键、具体用途、记忆/来源版本及可空Token；attempt增加记忆数量及筛选结果。
旧行保持未知，不伪造attempt或0 Token；新行不保存display_summary/正文。多条记忆的整次调用Token不能重复累计。
不改变记忆确认状态、RLS或向量数据；保持运行角色必要权限，不提升超级用户。
降级前停新版Worker并导出审计；downgrade018删除新增审计字段但不删除记忆正文。
详见 [7.4实现与部署](../../aegis_agent_worker/文档/阶段1/19-按模型用途选择与注入记忆-阶段7.4.md)。业务库未自动迁移。

### 019：日历结构化记忆

停止Agent Worker及API写入、备份，执行 `python -m alembic upgrade 019_calendar_memory_preferences` 后更新API/Worker/前端。
扩展memory_items的meeting_preferences实体key，以及使用审计的calendar_planning/calendar_draft_personalization/calendar_recommendation用途。
保留原生命周期、RLS、实体唯一性和Token约束；日历用途要求entity，不自动新增或确认用户记忆。
存在新实体（包括删除记录）或相关调用审计时拒绝降级；需人工导出处理，不自动删除。
迁移及真实SQL已在随机临时schema/受限角色内回滚验证，未迁移业务库。详见[日历记忆](../../aegis_agent_worker/文档/阶段1/20-日历逐步骤结构化记忆接入.md)。

### 020：记忆选择诊断、撤销与旧审计正文清理（当前版本）

先停止API写入及Agent Worker、备份数据库，再执行`python -m alembic upgrade 020_memory_selection_revoke`，然后统一更新API/Worker/前端。
父revision为019_calendar_memory_preferences；不要只迁移到旧文档的017/018/019后启动当前Worker。

新增run_execution_attempts.memory_selection_details（仅ID/版本/安全原因/数量，最大24000字节）与权限前置召回索引。
**清空全部历史run_memory_usages.display_summary正文副本**，保留memory_items源正文和ID/使用事实；增加禁止再次保存审计正文的约束及记忆变更清理触发器。
旧Worker若仍写此列将失败，不支持混跑。应用角色需要run_memory_usages的UPDATE及新列必要权限，保留现有RLS；不提升超级用户。

取消确认复用既有revoked状态，不新增自动确认、统一过期时间或时间衰减。
降级前停新版服务并备份；downgrade不恢复已清理的旧正文，恢复须用事前备份。未自动迁移业务库。
详见[7.5部署、选择与撤销](../../aegis_agent_worker/文档/阶段1/21-记忆优先级用户管理与撤销-阶段7.5.md)。

