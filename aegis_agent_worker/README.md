# Aegis Agent Worker

独立消费 Aegis Agent Redis 队列的 Python 服务。它不导入 `Aegis_backend` 的代码；两个服务共享 PostgreSQL 业务事实与 Redis 队列/实时事件协议。

当前任务由 LangGraph 根图执行：根图加载会话上下文、进行意图/工作流路由；日历读取作为 `calendar/read_graph.py` 子图执行，工具审计、事务与 SSE 事件仍由 `service/` 处理。

## 本地启动

1. 在本目录复制 `.env.example` 为 `.env`，填入数据库、Redis、模型配置。
2. 安装依赖：`D:\anaconda\envs\Aegis\python.exe -m pip install -e ".[dev]"`
3. 启动：`D:\anaconda\envs\Aegis\python.exe -m aegis_agent_worker`

API 服务仍在 `Aegis_backend` 中使用 Uvicorn 单独启动。

架构说明见：[文档/01-Agent与基础设施职责边界.md](文档/01-Agent与基础设施职责边界.md) 与 [文档/02-Agent图与工作流架构.md](文档/02-Agent图与工作流架构.md)。


