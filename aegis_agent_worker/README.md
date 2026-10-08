# Aegis Agent Worker

独立消费 Aegis Agent Redis 队列的 Python 服务。它不导入 `Aegis_backend` 的代码；两个服务共享 PostgreSQL 业务事实与 Redis 队列/实时事件协议。

当前任务由 LangGraph 根图执行：根图加载会话上下文、进行意图/工作流路由；日历读取作为 `calendar/read_graph.py` 子图执行，工具审计、事务与 SSE 事件仍由 `service/` 处理。

邮件摘要与回复草稿是独立的后台任务：主后端创建 `mail_extraction` 或
`mail_reply_draft` 类型的 `agent_runs` 后投递同一 Redis 队列；Worker 读取 Email MCP
正文，调用模型，分别写入 `mail_extractions` / `mail_extraction_todos` 或 `mail_drafts`，
并通过现有 run SSE 通道发布 `mail_extraction_completed` / `mail_reply_draft_completed`。
上述流程只生成站内数据，绝不会发送邮件。另有 `mail_send` 任务：草稿经主后端创建确认项、用户批准后，Worker 才调用 `mail.messages.send`；拒绝时草稿恢复为可编辑状态。

## 本地启动

1. 在本目录复制 `.env.example` 为 `.env`，填入数据库、Redis、模型配置。
2. 安装依赖：`D:\anaconda\envs\Aegis\python.exe -m pip install -e ".[dev]"`
3. 启动：`D:\anaconda\envs\Aegis\python.exe -m aegis_agent_worker`

若需要运行邮件摘要、回复草稿或批准后发送链路，还需启动 `Email_mcp_server`，并在本服务 `.env`
配置 `EMAIL_MCP_URL=http://127.0.0.1:9002/mcp`。邮件后台任务所需的数据库迁移由
`Aegis_backend` 执行：`D:\anaconda\envs\Aegis\python.exe -m alembic upgrade head`。

API 服务仍在 `Aegis_backend` 中使用 Uvicorn 单独启动。

架构说明见：[文档/01-Agent与基础设施职责边界.md](文档/01-Agent与基础设施职责边界.md) 与 [文档/02-Agent图与工作流架构.md](文档/02-Agent图与工作流架构.md)。


