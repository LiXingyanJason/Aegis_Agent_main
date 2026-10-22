# Aegis PA：面向邮件、日程与知识查询的个人效率 Agent 平台

Aegis PA 围绕邮件处理、会议安排与公司资料查询，提供自然语言交互、领域专家协作和人工确认能力。项目采用独立 API 与 Worker 架构，通过 LangGraph 管理任务状态与子图执行，结合 Agent Harness 统一约束模型和工具调用，支持缺失信息补问、检查点恢复、带来源回答及全过程进度展示。

当前实现、典型示例及验收边界见[第 01 期当前功能与使用示例](第01期-项目预期功能与使用示例.md)；运行组件见[当前技术栈](技术栈.md)。

### API、Worker 与 MCP 服务的关系

```text
前端 ── REST / SSE ──> FastAPI 主后端
                         ├─ PostgreSQL：业务数据、任务、事件、审批与索引任务
                         ├─ Redis：投递 Agent 任务和知识索引任务；通知 SSE
                         ├─ Calendar MCP Server：日历页面的只读查询
                         └─ Email MCP Server：邮件页面的读取与列表同步

Redis Agent 队列 ──> Agent Worker
                      ├─ LangGraph：路由、领域工作流、补问与确认恢复
                      ├─ PostgreSQL：读取任务、保存结果/审计和 checkpoint
                      └─ Tool Gateway ──> Calendar / Email MCP Server

Redis 知识索引队列 ──> 知识索引 Worker
                          ├─ 本机源文件 + PostgreSQL：读取待索引资料和任务状态
                          └─ Embedding 服务 + Qdrant：生成并保存知识向量
```

主后端验证用户身份、检查数据归属并创建任务；模型推断与工作流由独立的 Agent Worker 执行。API 和 Worker 通过 PostgreSQL 共享权威状态，通过 Redis 队列交接任务。运行事件先写入 PostgreSQL，再由 Redis 通知 API 推送 SSE；浏览器断线后可从数据库补发。

两个 MCP Server 是日历、邮件工具的适配进程，不负责用户登录、租户权限或任务审批。主后端仅为页面读取直接调用它们；Agent Worker 通过受控 Tool Gateway 调用读取工具，并在用户确认后调用相应写入工具。知识索引 Worker 只处理资料索引，不调用日历或邮件 MCP。当前日历与邮件 MCP 均使用本机 JSON Mock provider。

### Agent 核心设计

#### 1. 分层 Agent 与结构化路由

采用“主决策 Agent → 领域工作流 → Service → Tool Gateway”的执行链。主 Agent 根据用户请求和注册能力目录生成结构化决策，经候选能力、目标引用与工作流注册校验后，交接给邮件、日历或知识专家。领域 Agent 负责语义规划，Service 负责权限、版本和业务事实，Repository 负责数据库访问，工具网关负责受控执行。

页面按钮发起的明确任务直接进入对应工作流，补问和审批通过原检查点恢复，避免重复分类造成目标漂移。结构化输出失败时执行有界修复，修复调用仍计入预算；网络失败和预算耗尽不按格式错误重试。主对话中的邮件请求先返回可操作卡片，用户点击按钮后才启动摘要或起草任务。

#### 2. Agent Harness 与持久化预算

通过 `HarnessExecutor`、`GuardedLLMClient` 和 `ToolGateway` 建立统一执行边界，覆盖工具白名单、输入输出大小、模型/工具调用次数、累计活跃时间、单次超时及调用审计。每次真实调用前，在 PostgreSQL 短事务中原子占用预算并创建 attempt 记录；执行网络请求时不持有数据库事务，结束后记录结果、耗时与供应商返回的 Token 用量。

预算绑定任务与策略版本，补问或审批恢复后继续累计，用户等待时间不计入活跃耗时。全局预算、局部循环次数和输出修复上限集中配置，模型不能通过重新规划或重新委派获得额外额度。字符上限用于上下文治理，供应商缺失的 Token 用量保留为 unknown，不将字符数冒充计费用量。[Harness 实现](aegis_agent_worker/src/aegis_agent_worker/harness/executor.py)。

#### 3. 受限 ReAct 与信息补全

邮件起草采用“决策 → 只读工具调用／向用户提问 → 观察结果 → 再决策”的有界循环。模型根据原邮件、工具证据和用户回答选择下一步，可以查询相关邮件和日历；缺少价格、承诺等业务决定时暂停提问，信息充分后进入草稿生成。

每轮决策受 Schema 约束，只开放规定动作和工具。邮件读取目标必须来自原邮件或已返回的有效引用；循环同时受轮次、证据大小和总调用预算限制。模型提出工具调用计划后，由服务构造受控调用并经网关校验执行。[信息收集子图](aegis_agent_worker/src/aegis_agent_worker/agent/workflows/mail/collection_workflow.py)。

#### 4. Human-in-the-loop 与检查点恢复

基于 LangGraph `interrupt` 和 PostgreSQL Checkpointer 实现补问、邮件单选与外部写入确认。补充问题持久化后，通过 SSE 通知前端；用户回答绑定具体问题和任务，Worker 恢复同一 `run_id` 并沿用原预算。恢复后重新核验邮件来源、版本及相关日历事实，避免使用等待期间已变化的数据。

补充信息与批准写入使用独立接口和状态。回答参会人或会议时间只补全任务条件；日历创建、邮件发送仍须核对冻结版本并单独确认。写入前继续执行归属、状态、版本与幂等检查；草稿修改发生版本冲突时拒绝覆盖。

#### 5. 协调者与领域专家协作

实现协调 Agent、邮件专家和日历专家组成的受限团队。协调者根据已返回的结构化结果决定下一次委派；邮件专家自主检索、选择和整理会议要求，日历专家自主查询候选时间、补问并保存会议草稿，最终接入人工确认流程。该能力由 `COOPERATION_ENABLED` 开关控制。

例如“请按续约邮件中的时间要求查询日历并安排会议草稿”，可经历邮件检索、多封邮件单选、会议条件提取、日历查询、草稿生成及用户确认。团队共享有界的目标、结构化结果和来源引用，专家保留各自局部上下文；所有委派共用任务身份与 Harness 预算，协调者无权批准外部写入。协作通过专家结果反馈逐步推进。[协作工作流](aegis_agent_worker/src/aegis_agent_worker/agent/workflows/cooperation/team_workflow.py)。

#### 6. 上下文与记忆治理

分别管理会话历史、卡片引用、专家证据和长期偏好。主 Agent 可以按需调用受控只读工具读取卡片，再由代码核验快照、目标与版本；团队上下文只传递完成当前子目标所需的结果。长期记忆由用户主动维护，在允许的模型用途调用前统一筛选租户、归属、确认状态、敏感级别、有效期、来源状态与任务相关性。

普通回复和邮件措辞可以使用相关偏好；主路由、邮件事实提取和知识证据回答默认不注入个性化记忆。日历日期规划只读取结构化时区，草稿与推荐可有限使用软偏好。恢复任务会重新选择记忆，已删除或撤销确认的记录不能凭旧 checkpoint 再次进入 Prompt。[记忆上下文服务](aegis_agent_worker/src/aegis_agent_worker/service/memory/memory_context_service.py)。

#### 7. 权限前置的 Hybrid RAG

KnowledgeAgent 负责受限查询规划和证据回答，Service 执行 PostgreSQL 全文检索与 Qdrant 向量检索，并通过 RRF 融合排序。召回前由 SQL 确定可访问且已启用的 ready 版本范围；向量结果返回后再次核验引用并读取片段，回答关联文档、版本和片段来源。

Embedding 使用独立通信适配、用途策略、预算与审计。无有效证据时返回无法确认；向量服务异常仅在显式策略允许时降级，并保留降级标识。Qdrant payload 不作为授权证明，个人记忆不能充当公司制度证据。[Hybrid 检索服务](aegis_agent_worker/src/aegis_agent_worker/service/knowledge/hybrid_retrieval_service.py)。

#### 8. 协作可视化与评测

将主路由、专家委派、工具执行、等待补充、等待确认和失败原因记录为持久化事件，通过 SSE 展示协作过程。Redis 提供事件通知，PostgreSQL 支持历史回放；调用审计关联模型用途、attempt、策略版本、耗时与供应商 Token 用量。结构化输出诊断只保存安全字段路径和错误类型，不保存原始非法响应或模型思维过程。

已建立固定路由评测集与故障注入测试。历史版本对比中，两版在 53 条真实语义用例上均为 50/53（94.34%），该结果不能用于宣称准确率提升或生产验收通过。另已跑通真实模型、数据库、Redis 与 Mock MCP 的两参会人协作链路，完成补问、草稿确认及一次 Mock 日历写入。参见[路由评测报告](aegis_agent_worker/文档/阶段1/09-主决策路由评测实验报告-2026-10-14.md)与[协作链路测试报告](aegis_agent_worker/文档/阶段1/23-协作真实链路测试报告-2026-10-21.md)。

当前实现尚未包含 Skills 动态装载、专家并行执行或真实企业邮箱/日历 provider。Mock 日历查询只反映当前用户的日历情况，不能据此断言所有参会人均空闲。

### 技术栈

| 组件 | 当前使用的技术 | 实际用途与状态 |
| --- | --- | --- |
| 前端 | Vue 3、**JavaScript**、Vite、Vue Router、Pinia、Element Plus | 对话、日历、邮件、知识资料、长期记忆、待办和操作确认页面。当前源码为 `.js`/`.vue`，未采用 TypeScript。 |
| 浏览器通信 | Axios；原生 `fetch` + 流式 SSE 解析 | Axios 调用普通 API；任务事件需要 Authorization 请求头，故使用 `fetch` 读取 SSE，并通过 `Last-Event-ID` 续传，不直接使用浏览器 `EventSource`。 |
| 身份认证 | Keycloak JS、OIDC、PKCE；后端 PyJWT + JWKS | 前端登录，后端通过 OIDC Discovery 获取公钥并验证 access token 的签名、issuer、audience 等声明，再映射本地用户。 |
| 主后端 | Python >=3.12、FastAPI、Uvicorn、Pydantic / pydantic-settings、httpx | 提供 REST、SSE、文件上传、请求校验、配置和 MCP 页面读取。 |
| 数据库访问 | PostgreSQL、SQLAlchemy 2 Async、asyncpg、Alembic | 持久化会话、任务、邮件快照/草稿、日历草稿/审批、记忆、知识资料、索引任务及审计；迁移文件当前到 `021_structured_input_choices`。租户/用户上下文与 PostgreSQL RLS 共同限制读取。 |
| 异步任务与实时通知 | `redis.asyncio`；Redis List、Pub/Sub | API 提交 Agent/索引任务；两个 Worker 消费各自的队列；事件先持久化到 PostgreSQL，再以 Redis 通知 SSE。未使用 Celery。 |
| Agent 编排 | LangGraph、`langgraph-checkpoint-postgres`、psycopg 3 | 根图注册领域工作流，支持主决策路由、邮件/日历/知识子图、补问和审批恢复；checkpoint 落 PostgreSQL。可选启用邮件与日历的双专家协作。 |
| 模型调用 | 项目自建 OpenAI Chat Completions 兼容 HTTP 客户端、`GuardedLLMClient` | 聊天模型供应商由 Worker 配置；结构化输出经 Schema 校验。Harness 对真实模型/工具调用执行白名单、预算、超时与审计。已验证 DeepSeek 兼容配置；不依赖某个模型供应商的 Python SDK。 |
| MCP 工具 | MCP Python SDK `2.3.0`、自建 Tool Gateway、日历/邮件 MCP Server | 工具参数、连接范围、风险与确认在代码中校验；MCP 服务支持 streamable HTTP 或 stdio。当前两个服务均只实现 JSON 文件 Mock provider，尚非真实日历/邮箱连接器。 |
| 知识检索（可选） | PostgreSQL 全文检索、独立 Embedding 客户端、Qdrant HTTP API、RRF | 资料先做权限/有效版本过滤，再分别检索关键词与向量、融合排名，并回 PostgreSQL 复核来源。Embedding 与 Qdrant 均有独立配置开关和治理；未使用 pgvector。 |
| 文件存储 | 受控本机目录 | 知识资料源文件以服务器生成的 UUID key 保存；元数据、片段与权限事实在 PostgreSQL。当前没有 OSS 适配器。 |
| 可观测性 | OpenTelemetry、JSON 结构化日志 | API/Worker 的 trace context、HTTP/SQLAlchemy/Redis 埋点与安全日志；控制台 Span 导出可选。仓库未接入 Prometheus/Grafana/Loki/Alertmanager 服务配置。 |
| 测试 | pytest、pytest-asyncio、httpx；前端 Node.js 内置 `node:test` | Python 单元/API/迁移测试与前端 JS 测试。当前前端脚本没有 Vitest 或 Playwright。 |
