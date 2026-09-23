# Aegis PA 系统概要说明书

## 1. 目标与范围

### 1.1 目标

构建一个生产级个人助理 Agent，在用户授权范围内完成日程管理、邮件处理、信息查询与受控代码执行；通过长期记忆提供个性化结果，并提供可审计、可观测、可回滚的外部操作链路。

### 1.2 范围边界

| 在范围内 | 不在范围内 |
|---|---|
| 读取/创建/修改日历事件 | 无确认的外部写入或付款 |
| 邮件检索、摘要、起草、确认后发送 | 规避组织安全策略、绕过账号权限 |
| 联网信息查询、带来源摘要 | 将搜索结果视为事实或擅自执行建议 |
| 沙箱代码执行与结果返回 | 访问宿主机、内网、用户凭据或持久化文件 |

## 2. 架构

```text
Client
  │ HTTPS/SSE
FastAPI API Gateway ── Auth/RBAC/Rate limit
  │                         │
  ├─ LangGraph Orchestrator ─┼─ NeMo Guardrails（输入、规划、输出）
  │   ├─ Intent & Risk Router│
  │   ├─ Model Router        ├─ 小模型：分类、抽取、摘要
  │   ├─ Memory Retriever    └─ 大模型：复杂规划、推理、写作
  │   ├─ Tool Planner / MCP Client
  │   └─ Confirmation Gate
  │
  ├─ MCP Tool Gateway ─ Calendar / Email / Search / Sandbox
  ├─ Redis（会话、短期上下文、幂等键、限流）
  ├─ PostgreSQL + pgvector（记忆、实体、任务、审计）
  └─ LangSmith traces + Prometheus metrics → Grafana/Alertmanager
```

### 2.1 LangGraph 状态机

`RECEIVED → INPUT_GUARD → INTENT_ROUTE → MEMORY_RETRIEVE → PLAN → POLICY_CHECK → [CONFIRM] → TOOL_EXECUTE → VERIFY → MEMORY_WRITE → OUTPUT_GUARD → RESPONDED`

- `CONFIRM` 仅在风险等级为 `write`、`sensitive` 或 `code_execute` 时进入。
- 每个节点向 LangSmith 写入 trace；状态与断点写入 Redis，服务重启后可恢复。
- 工具失败按错误类型有限重试；不可重试错误转为可读的补救建议，不由模型臆造成功结果。

## 3. 核心模块设计

### 3.1 智能路由

路由器输出 `intent`、`complexity`、`risk_level`、候选工具和置信度。低复杂度、无工具或只读任务优先走小模型；需要多步规划、跨工具协调、长文生成或低置信度任务走大模型。模型降级必须记录 `model_route`、token、时延与成本。

### 3.2 工具调用与 MCP

工具以 MCP Server 接入，注册表维护名称、JSON Schema、权限、风险级别、超时与版本。调用顺序为：发现 → 参数 Schema 校验 → 授权 → 确认（若需）→ 执行 → 标准化结果/错误 → 审计。

### 3.3 记忆

- **短期**：Redis 中按会话保存近轮消息、图状态和未确认动作，设置 TTL。
- **长期**：把高价值交互压缩为摘要，按用户、作用域、敏感级别存 PostgreSQL；embedding 写 pgvector。
- **实体**：如时区、常用联系人、工作偏好，存结构化事实并记录来源与置信度。
- **检索**：先进行租户与权限过滤，再做向量近邻与时间/重要性重排；默认不将敏感记忆注入提示词。
- **衰减**：访问频率、置信度和时间共同计算分数；过期内容归档，用户可查看、编辑和删除。

### 3.4 安全与可观测性

NeMo Guardrails 在输入、计划和输出三层运行；策略服务负责 RBAC、OAuth Scope、工具 allowlist、确认令牌及审计。LangSmith 用于 LLM/Agent trace，Prometheus 用于系统指标；两者以 `request_id` / `trace_id` 关联。

## 4. 非功能需求

| 类别 | 目标 |
|---|---|
| 可用性 | API 月可用性 ≥ 99.9%，无状态 API 可水平扩展 |
| 性能 | 不含外部工具时 P95 首字响应 < 2 秒；工具任务展示流式进度 |
| 可靠性 | 写操作使用幂等键；工具调用设置超时、重试、熔断与死信记录 |
| 隐私 | 按用户隔离数据；凭据仅由密钥管理系统保存；审计记录脱敏 |
| 成本 | 每请求记录模型 token 和估算成本；路由命中小模型比例、节省率可观测 |

## 5. 部署概要

Docker 镜像包含 API、Worker 与 migration job。Kubernetes 中 API/Worker 使用 HPA；Redis、PostgreSQL 采用托管服务或 StatefulSet；MCP Server 独立部署并以 NetworkPolicy 隔离。通过 Helm 管理环境变量、Secret 引用、资源限制与滚动发布。
