# Aegis Agent Worker

独立消费 Aegis Agent Redis 队列的 Python 服务。它不导入 `Aegis_backend` 的代码；两个服务共享 PostgreSQL 业务事实与 Redis 队列/实时事件协议。

当前任务由 LangGraph 根图执行：根图加载会话上下文、进行意图/工作流路由；日历读取作为 `calendar/read_graph.py` 子图执行，工具审计、事务与 SSE 事件仍由 `service/` 处理。

普通对话由独立 `MainDecisionWorkflow` 子图组织：读取引用 → MainAgent 调用 LLM → 能力校验 → 目标核验 → 组装 handoff → 保存审计。LLM 推理和流程分支位于 `agent/workflows/main/decision_workflow.py`；`DecisionContextService` 只负责业务引用、归属／版本检查及事务性审计。根图注册主决策子图并根据其结果分流，领域工厂显式声明可用意图。原关键词路由保留为旧组件，但不再负责生产根图的普通对话决策。页面指定任务与审批恢复均绕过主决策子图。

当前支持日程查看、空闲查询、会议安排，以及明确邮件的摘要和回复起草。邮件目标来自本人已同步的最近 20 条快照，模型只能选择提供的 ID 与 source_version；邮件检索、草稿修改、复合任务暂返回澄清。先在邮件管理页面同步收件箱，再在主对话中指定邮件主题；若多个候选有歧义，需补充目标。

决策事件 `main_decision_completed` 持久化到 run_events 并经 SSE 通知，包含意图、目标引用、能力版本、模型信息、耗时及校验结果；不含正文或模型思维过程。模型不可用时任务失败，非法输出时返回澄清。没有新增配置或迁移，使用原模型配置，修改后重启 Worker。

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


