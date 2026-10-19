# Aegis Agent Worker

阶段4.1的生产路由、双入口和恢复回归基线见
[路由基线说明](文档/06-主决策路由与双入口回归基线.md)。对话能力来自领域Factory注册，
页面明确任务和检查点恢复不经过主Agent重新分类；旧关键词Router链、无调用预留Router
及无用构造参数已移除。现有接口、数据库和恢复行为不变。

阶段4.2已接入`agent/routing/CapabilitySelector`（文件`capability_selector.py`）。
只对简单明确浏览收敛候选领域，复杂、指代、无信号或跨领域请求保留全部注册能力。
主Agent只接收候选能力，输出还须通过候选集与注册目录双重检查；连接和审批仍由领域服务校验。
候选意外为空恢复目录，目录本身为空则澄清。审计新增候选标识、稳定原因码和策略版本，
无新增数据库迁移，重启Worker即可。

阶段4.3：主Agent调用JSON mode并进行MainDecision校验，格式错误最多修复一次，两次均计预算。
连续失败转澄清而非默认执行；网络／预算错误不按格式错误重试。主路由审计新增repair_count和
validation_issue_codes，不保存非法响应和含敏感值的校验异常。邮件“第N封”仅对最近有效卡片顺序解析，
缺失／过期／位置或版本不符则澄清，不能用收件箱排序替代。修改后重启Worker，无新增迁移。

阶段4.3A已将卡片指代改为按需内部工具：conversation.cards.list/get。
主Agent可提出read_context请求，主决策子图通过MainContextToolService→ToolGateway→Harness→
CardContextToolHandler→CardContextService读取当前run的站内卡片。
不自动预取邮箱引用、不用_mail_ordinal解析自然语言；模型输出card_reference后按快照及当前版本解析target。
每次主决策最多2次卡片读取、3轮模型决策（格式修复每轮最多1次），总预算仍不重置。
工具只支持mail_candidates，不返回邮件正文／凭据、不调用外部邮箱／日历。
旧审批和补充回答恢复节点保持不变；先让当前消费结束再重启Worker，无需迁移或删除Checkpoint。

全局／局部／输出限制现已集中到harness/policies，由PolicyProvider按run固定的策略包版本提供。
默认harness-v1数值不变；恢复不会因新的默认策略获得额外额度。
修改限制须注册新版本并保留旧定义，见[执行与工作流限制集中策略](文档/07-执行与工作流限制集中策略.md)。

独立消费 Aegis Agent Redis 队列的 Python 服务。它不导入 `Aegis_backend` 的代码；两个服务共享 PostgreSQL 业务事实与 Redis 队列/实时事件协议。

当前任务由 LangGraph 根图执行：根图加载会话上下文、进行意图/工作流路由；日历读取作为 `calendar/read_graph.py` 子图执行，工具审计、事务与 SSE 事件仍由 `service/` 处理。

日历读取和会议确认现在先进入 `CalendarPlanningWorkflow`，由 `CalendarAgent.plan_calendar` 调用 LLM 生成受控 CalendarDecision，校验后再执行工具。旧同步关键词规划方法仅作为兼容入口保留，生产图不调用。指定起止时段（包括凌晨）使用日程查询做确定性冲突检查；无冲突时创建该时段的待确认草稿，并提示非工作时间。自动推荐仍使用 MCP 空闲候选。冲突检查目前仅覆盖 Mock 当前用户日历。

没有新增环境变量或数据库迁移；使用既有模型配置，更新后重启 Worker。测试示例：发送“请在2026-10-15 03:00～03:50安排50分钟总结会议”，在该时段无日程时应产生凌晨三点的草稿，而非上午十一点或默认工作时段。

日历规划输出校验失败时最多调用模型修复一次，修复结果仍重新核验 Schema、原始指定时间、时长与交接范围。两次均失败时不调用日历工具。Worker 日志记录字段路径和错误类型，如 `preferences:string_type` 或 `CALENDAR_EXPLICIT_TIME_MISMATCH`；`calendar_plan_completed.validation_issues` 也保存同样的脱敏诊断，不记录原始模型响应或邮箱。模型请求超时等网络故障不走格式修复。

普通对话由独立 `MainDecisionWorkflow` 子图组织：读取引用 → MainAgent 调用 LLM → 能力校验 → 目标核验 → 组装 handoff → 保存审计。LLM 推理和流程分支位于 `agent/workflows/main/decision_workflow.py`；`DecisionContextService` 只负责业务引用、归属／版本检查及事务性审计。根图注册主决策子图并根据其结果分流，领域工厂显式声明可用意图。原关键词路由保留为旧组件，但不再负责生产根图的普通对话决策。页面指定任务与审批恢复均绕过主决策子图。

当前支持日程查看、空闲查询、会议安排，以及主对话邮件列表／单封操作卡片。邮件查询由 MailQueryWorkflow 经 MailPlanningWorkflow 生成查询条件，再经 Gateway 读取元数据、同步快照并筛选，保存 mail_candidates Artifact，通过 artifacts_ready 通知前端。主对话不直接生成邮件摘要或回复；用户必须点击卡片／页面按钮发起明确任务。

后续目标优先来自当前会话最近的邮件候选卡片，按 position 保持显示顺序并核验 source_version；没有查询卡片时才提供最近20条已同步快照。空、过期或版本失效候选不能用其他收件箱邮件替代。草稿修改及复合任务仍需后续实现。

前端需新增 mail_candidates 卡片类型，复用 GET /conversations/{conversation_id}/artifacts 与现有邮件详情、摘要、待办、起草接口。本次不修改前端代码。查询默认展示最多10条，可选择最多20条，数量／字节截断通过 matched_count、returned_count、truncated 明示。先测试“查询全部邮件”，再测试“查找续约邮件”或“找本周续约邮件”；固定 Mock 日期不在本周时，空结果是正常情况。

邮件领域统一使用 `MailPlanningWorkflow → MailAgent.plan_mail → MailDecision`。普通对话仅规划 query／clarify，查询参数在 decision.query，可带已核验 decision.target 来定位单封卡片；摘要／起草／待办由按钮调用既有业务接口触发。模型提出 extract／draft_reply 时主路由会降级为 query_mail，生成子图也设有对话执行阻断。页面任务从可信请求记录构造 MailDecision，不再次调用模型分类。plan_query 仅保留兼容入口。

笼统“生成摘要”返回列表；完整主题或有效序号定位唯一邮件时返回 single 卡片。Artifact 新增 display_mode=list／single，前端已适配单封模式隐藏列表标题和计数栏。subject_equals 支持完整主题精确匹配。需要重启 Worker 并刷新前端，旧 Artifact 不会自动重新执行。

查询、摘要、起草子图均先经过统一规划，非法计划或澄清不会读取正文或生成草稿；后台请求无效时标记失败。发送确认不经过此规划子图，仍由专用审批恢复流程执行。新增 mail_plan_completed 审计事件，包含规划来源 llm／explicit_task、动作、目标引用和耗时，不包含正文。本次无需新增配置或迁移，重启 Worker 生效。

决策事件 `main_decision_completed` 持久化到 run_events 并经 SSE 通知，包含意图、目标引用、能力版本、模型信息、耗时及校验结果；不含正文或模型思维过程。模型不可用时任务失败，非法输出时返回澄清。没有新增配置或迁移，使用原模型配置，修改后重启 Worker。

邮件摘要与回复草稿是独立的后台任务：主后端创建 `mail_extraction` 或
`mail_reply_draft` 类型的 `agent_runs` 后投递同一 Redis 队列；Worker 读取 Email MCP
正文，调用模型，分别写入 `mail_extractions` / `mail_extraction_todos` 或 `mail_drafts`，
并通过现有 run SSE 通道发布 `mail_extraction_completed` / `mail_reply_draft_completed`。
上述流程只生成站内数据，绝不会发送邮件。另有 `mail_send` 任务：草稿经主后端创建确认项、用户批准后，Worker 才调用 `mail.messages.send`；拒绝时草稿恢复为可编辑状态。

## 本地启动

### 阶段3部署与联调

先在 Aegis_backend 执行 `python -m alembic upgrade head`，迁移至 `008_agent_harness`，再重启API与Worker。无需新增依赖或环境变量，使用现有模型／Redis／MCP配置。预算记录和用户补充状态存入新表；模型调用、工具尝试和活跃耗时跨恢复保留。

当前默认上限：10次模型调用、6次工具尝试、120秒活跃时间、5轮ReAct决策；模型单次60秒、工具20秒。输入上下文上限48000字符、模型输出80000字符；这是字符预算，不是实际Token计费。工具输出额外受字节限制。等待用户不计时，问题24小时过期。

Harness已按职责分层：`harness/`管理策略、上下文和执行编排，
`repository/execution_repository.py`管理SQL，`llm/guarded_client.py`包装模型调用。
详见[Harness说明](src/aegis_agent_worker/harness/README.md)。原`service/runtime/execution_harness.py`
已拆分删除；补充问答仍保留在运行期业务服务。API、数据库与默认预算不变，无新增迁移。

信息收集与恢复后的事实核验，每次工具调用都创建独立 `reply_collect_<UUID>` 步骤键，分别关联 `tool_calls`。不要复用固定 `reply_collect`，也不要移除 `run_steps(run_id, step_key)` 唯一约束；修复不需要数据库迁移，重启 Worker 后重新提交失败任务即可。

邮件信息收集、摘要、回复生成及草稿修改使用 `response_format={"type":"json_object"}` 请求 JSON 输出（[DeepSeek 官方说明](https://api-docs.deepseek.com/guides/json_mode/)），再由本地业务 Schema 校验。JSON mode 不等于业务校验。文字、空响应或非法字段最多修复一次，两次调用均计入 Harness 预算；网络错误、权限错误、预算耗尽不进行格式修复。不支持 JSON mode 的兼容供应商会返回受控调用失败，不静默关闭约束。连续校验失败使用 `MODEL_OUTPUT_INVALID`；不保存／回传原始非法输出，不继续生成草稿或执行工具。修改后只需重启 Worker，无新增迁移或前端要求，失败的旧任务需重新提交。

起草回复先进行有界只读信息收集，可查相关邮件及当前用户日历；缺少价格等信息时发布 input_required 并暂停，前端提交回答后以相同 run_id 恢复。主对话仍只返回邮件卡片，ReAct从卡片“起草回复”按钮启动。完整正文只在读取／生成节点局部使用，图状态保存安全摘要。

草稿窗口 AI 修改通过 revision-requests 创建独立任务，只改已保存draft状态草稿正文；保存前后都核验版本。版本冲突、模型失败不会覆盖原文。pending_approval和已发送草稿不允许AI原地修改。

失败任务的 run_id／error_code 写到队列名加 `:dead-letter` 的Redis List，不自动重放外部写入。当前仍是BLPOP消费；进程在取出任务后崩溃的队列回收及Redis Streams未实现，需要运维结合数据库检查恢复，不能把List宣称为已保证可靠交付。

1. 在本目录复制 `.env.example` 为 `.env`，填入数据库、Redis、模型配置。
2. 安装依赖：`D:\anaconda\envs\Aegis\python.exe -m pip install -e ".[dev]"`
3. 启动：`D:\anaconda\envs\Aegis\python.exe -m aegis_agent_worker`

若需要运行邮件摘要、回复草稿或批准后发送链路，还需启动 `Email_mcp_server`，并在本服务 `.env`
配置 `EMAIL_MCP_URL=http://127.0.0.1:9002/mcp`。邮件后台任务所需的数据库迁移由
`Aegis_backend` 执行：`D:\anaconda\envs\Aegis\python.exe -m alembic upgrade head`。

API 服务仍在 `Aegis_backend` 中使用 Uvicorn 单独启动。

### 文档索引进程（6.3）

索引由单独进程消费，不阻塞上述Agent循环。先由主后端升级至013迁移，确保API和Worker的
源文件根目录、Embedding模型元数据一致，且Worker已配置Embedding和Qdrant写密钥。

```powershell
D:\anaconda\envs\Aegis\python.exe -m aegis_agent_worker.worker.index_worker
```

该进程从专用Redis队列接收job_id/身份封包，并扫描SQL中未投递或租约过期任务。
上传只保存文件，不排队；需所有者显式启动索引。索引成功后默认禁用，需再显式启用。
启动后处理真实任务会调用计费Embedding并写入远端Qdrant；不需要启动Calendar/Email MCP。
它不是Agent聊天任务的可靠恢复改造；上方Agent List崩溃恢复限制仍然存在。
详见[6.3导入与索引](文档/12-知识资料导入与索引-阶段6.3.md)。

### 知识查询（6.4）

已接入PostgreSQL全文＋受治理Embedding＋Qdrant只读召回及RRF，默认关闭。
启用前需资料ready且enabled，查询模型空间与索引一致，配置Qdrant读取密钥，
仅在已获外发授权时设置`EMBEDDING_EXTERNAL_QUERIES_ALLOWED=true`及`KNOWLEDGE_QUERY_ENABLED=true`，重启聊天Worker。
`KNOWLEDGE_LEXICAL_FALLBACK_ALLOWED=false`默认不降级；不新增浏览器直调检索接口。
本次仅离线及可回滚PG验证，未调用真实云端。详见[6.4配置与验收](文档/13-权限前置Hybrid检索与RRF-阶段6.4.md)。

6.5已补齐来源引用修复／冲突提示、生成前后复核及主后端历史来源卡片的即时权限检查；
无新迁移，启用方式不变。详见[知识子图与来源约束回答](文档/14-知识查询子图与来源约束回答-阶段6.5.md)。

6.6已接通资料管理接口、来源详情、阶段SSE及前端来源卡片。知识资料类别功能暂缓，
上传接口与查询计划不再接收类别字段，已有资料不需重新索引；部署前先升级至016_knowledge_category_default，
再一起更新API／Agent Worker／Index Worker和前端。详见[6.6部署与手工联调](文档/15-知识资料接口-SSE与来源卡片-阶段6.6.md)。

架构说明见：[文档/01-Agent与基础设施职责边界.md](文档/01-Agent与基础设施职责边界.md) 与 [文档/02-Agent图与工作流架构.md](文档/02-Agent图与工作流架构.md)。

### 会话工作缓存（7.2）

`SESSION_MEMORY_ENABLED=false`默认关闭；显式设为true后重启Agent Worker，缓存有界任务状态/卡片引用，
不存消息或业务正文。SQL继续核验归属与版本，Redis故障回退SQL。API与前端新增退出前清缓存，
不删除持久消息或长期偏好，无新增迁移。详见[7.2部署与验证](文档/17-会话工作记忆Redis有界缓存-阶段7.2.md)。

### 手动长期摘要与实体记忆（7.3）

长期记忆页面支持保存待确认、版本确认/编辑与显式实体替换；聊天/专家没有记忆写工具。
需先停旧Agent Worker与API写入、执行017_memory_manual_lifecycle，再更新API/Worker/前端，不能与旧读取逻辑混跑。
SQL过滤确认状态、敏感、有效期与来源；普通回复模型调用前重读，邮件起草保留生成前读取。
无向量重建、无自动提取。详见[7.3部署与契约](文档/18-用户主动维护长期摘要与实体记忆-阶段7.3.md)。


