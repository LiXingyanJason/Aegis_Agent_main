# Skill 按需加载与个人管理

实施日期：2026-10-24。此实现替代撤回的 Skill 工作流映射方案。

## 1. 范围

Skill 保存“这类任务怎样处理”的指南与模板。基础规则、工具权限、Schema、事实复核仍由现有代码维护。
长期记忆描述用户偏好；RAG 文档是业务事实来源，两者都不会被自动复制成 Skill。

首版用途仅为 `mail_reply_draft` 和 `mail_draft_revision`。普通对话、主路由、邮件事实提取、信息收集、
日历规划、知识回答、协作路由均不读取 Skill。没有候选的调用不额外请求模型。

新增独立管理页面的需求，使存储方案调整为：内置 Markdown 模板随 API 包部署；个人草稿、发布版本和状态存 PostgreSQL。
Worker 读取已发布的个人指南，不依赖共享文件系统，也不允许模型或接口指定服务器路径。
内置文件位于 `Aegis_backend/app/assets/skills/renewal-reply/SKILL.md`，JSON frontmatter 是 YAML 的受限子集；
只解析项目维护的固定模板，不开放通用 YAML/Markdown 上传解析。编辑接口采用结构化字段，`body` 为 Markdown。

## 2. 模块与职责

| 模块 | 职责 |
|---|---|
| API `param/skill_param.py` | 名称、用途、大小、未知字段与并发版本校验 |
| API `controller/skill_controller.py` | 认证 HTTP 接口，响应包装，禁止缓存 |
| API `service/skill_service.py` | 草稿/发布/启停/删除流程、固定模板与安全错误 |
| API `repository/skill_repository.py` | 归属查询、锁行更新、目录上限、发布版本与审计 SQL |
| Worker `agent/skills/contracts.py` | 指南内容、选择和工具参数契约及一致的内容哈希 |
| Worker `agent/skills/selector.py` | 有界任务摘要和单轮目录选择，不提供工具权限 |
| Worker `service/skills/skill_context_service.py` | 持久绑定、工具编排、每次调用复核与使用记录；Skill 服务统一放在 `service/skills/` 目录 |
| Worker `repository/skill_repository.py` | 候选、选择占位、读取额度、版本读取与使用 SQL |
| `GuardedLLMClient` | 统一调用接入点，模型预算、记忆、Skill、用量及超时 |
| `SkillPolicy` | 20 个候选、6000 正文字符、4000 任务摘要字符、一次选择/读取 |

首版一次选择/读取是固定协议约束，数据库也限定 reads≤1；若后续放宽，需要新策略版本及对应迁移，不能只改默认值。
Skill 不扩展 GraphRegistry/TeamRegistry，不注册新的业务执行能力。

## 3. 调用链

1. 已有邮件工作流调用 MailAgent，沿用 `mail_reply_draft` 或 `mail_draft_revision` 用途。
2. GuardedLLMClient 检查当前任务固定策略是否开放 Skill。
3. 按 run/用途原子登记 `selecting`，查询当前用户已发布启用且适用的候选，仅返回名称、描述与稳定版本元数据。
4. 有候选时以 `skill_selection` 发起一次受 Harness 预算控制的 JSON 调用。显式用户要求摘要优先于长邮件片段，
   不给选择器完整 Prompt 或长期记忆。模型返回候选 ID 或 null。
5. 固定所选 ID、版本、哈希；无候选、不适用或非法模型选择记录原因并使用基础能力继续。
6. 选中后构造 ToolInvocation，由 ToolGateway 校验 `skills.read` 参数与任务工具白名单，Harness 扣减工具额度、限制超时并审计。
7. 工具读取受归属及已选绑定限制，模型不能读取其他条目或提供文件路径。工具输出摘要不包含正文。
8. 每次真正的草稿/修改调用及格式修复前，重新核验数据库状态、版本哈希和既有读取记录。
9. 将指南 JSON 放在 user 消息中、原始本轮要求之前；明确其优先级低于用户要求、基础规则与证据。
10. 在发起模型通信前写 `run_skill_usages`，关联真实 attempt。Token/耗时来自该 attempt，缺失保持 null。

数据库中的版本复核是内部授权/完整性检查，不是让模型重复调用读取工具；不会重置局部次数。
指南与长期记忆分别选择，均计入总字符限制。两者同时存在时仍以本轮明确要求和基础规则为准。
首版已选指南与 prepared 消息工厂组合会明确拒绝，防止工厂重建消息后漏掉已经审计的指南；当前邮件接入不使用该组合。

## 4. 生命周期与恢复

- 创建：`revision=1`，未发布，`enabled=false`。
- 发布：把当前内容保存为不可变版本，`published_version=发布前revision`，启用，revision 加一。
- 编辑：更新待发布正文并增加 revision，原已发布版本仍供新任务读取，直到再次发布。
- 新发布版本：新任务选新版本；已有绑定继续旧版本，不因恢复或修复自动切换。
- 停用/删除：每次后续模型调用前查询可用性，所选 Skill 失效则以 `SKILL_VERSION_UNAVAILABLE` 停止。
  已发往供应商的请求无法追溯撤销；数据库复核之后的并发撤销窗口与其他上下文复核相同。
- 删除为软删除，保留不可变历史与使用审计，不代表物理擦除内容。删除条目不再通过普通详情/版本接口读取。
- 选择已经占位但进程中断：返回 `SKILL_SELECTION_INTERRUPTED`，要求新任务，避免恢复时不断选择消耗预算。
- 工具读取之前发生预算/网络失败：按原 Harness/LLM 错误结束；不伪装成 JSON 格式错误重试。
- 已选版本丢失、篡改或工具不可用：明确失败，不悄悄使用其他版本。

`revision` 是编辑并发令牌，`published_version` 是内容版本，两者不能简单比较以判断有无草稿差异。
如需回到旧内容，可读取该版本、保存为新草稿再发布；原发布版本始终不可修改。
管理接口不接受 tenant/user、工具名、权限、脚本或工作流映射。首版只有个人范围，没有租户管理员共享发布。
任意自然语言“必须用某 Skill”的严格解析尚未作为产品契约开放；目前由用途候选选择，不新增任务参数或文本命令。

## 5. 表与审计

迁移 `022_skills` 在 021 之后新增：

- `skills`：归属、草稿内容、revision、发布指针、启用/删除状态。
- `skill_versions`：不可变内容和 SHA-256 哈希，数据库触发器禁止 UPDATE/DELETE。
- `skill_events`：创建、编辑、发布、启停、删除的操作者和 revision；无 Prompt。
- `run_skill_bindings`：一次选择/读取及原因，只有稳定引用，没有正文副本。
- `run_skill_usages`：每次实际尝试注入的版本、哈希、字符数、用途、attempt。

五表均启用 FORCE RLS，限制 tenant+user；Repository 同时显式限定归属。
模型选择与工具读取分别在 `run_execution_attempts` 中可见；实际注入记录通过 attempt 关联完整调用 Token 与耗时，
这些 Token 属于整次调用，不是 Skill 单独消耗。没有使用字符数推算计费 Token。
若通信失败，usage 中关联的 attempt 为 failed，不能展示为回答成功。
接口 `GET /skills/runs/{run_id}/usages` 同时返回绑定原因和实际注入记录，不返回指南或完整 Prompt。
本轮不新增 SSE 事件类型，现有任务状态/SSE 不变，管理页面可以按需调用使用记录接口。

## 6. 部署与使用

1. 先部署并执行 API 迁移：`python -m alembic upgrade 022_skills`。本次未对业务库执行迁移。
2. 部署新版 API、Worker；Worker 设置 `SKILLS_ENABLED=true` 并重启。
3. 管理页面调用 `/skills/templates` 获取内置模板，POST `/skills` 创建个人草稿，核对后 POST `/{id}/publish`。
4. 发起新的邮件起草或修改任务。没有已发布启用的个人 Skill 时不会调用选择模型。
5. 通过使用记录接口核对模型是否实际加载。旧任务保留旧策略，不会因开关变化新增 Skill 能力。

新策略名称为原版本加 `-skills-v1`，保留所有历史策略；原总模型/工具预算不增加，选择和读取会占用其中的额度。
关闭开关只影响新任务。已绑定新策略的任务恢复仍需要本版代码、022 表和历史发布版本。
降级前先处理完相关任务并导出 Skill 数据；迁移有数据时拒绝降级，不自动销毁资料。
安装包包含内置 Markdown 模板。前端只需接口对接，不需要读取 Worker 磁盘。

## 7. 工程验证与质量评测

已覆盖：候选外 ID、非法格式/null、用途隔离、路径参数拒绝、超大正文、旧策略不启用、无候选零额外调用、
模型预算失败、重复读取拒绝、格式修复两次 attempt、恢复继续原版本、删除/停用和哈希篡改。
API 覆盖认证、未知字段、版本冲突、未发布不能启用、保存不发布。
真实 PostgreSQL 使用随机 schema 和 NOSUPERUSER/NOBYPASSRLS 角色，验证实际 SQL、迁移、生命周期、版本绑定、
同租户其他用户/其他租户不可见及使用记录；结束后回滚，并确认 schema 和角色不存在，不修改业务表。

测试命令：

```powershell
# Worker
python -m pytest -q -p no:cacheprovider tests/agent/test_skills.py
# API
python -m pytest -q -p no:cacheprovider tests/controller/test_skills.py
# 仅本地开发PostgreSQL；随机临时schema和角色，事务回滚
$env:AEGIS_SKILL_DB_TESTS='1'
python -m pytest -q -p no:cacheprovider tests/migrations/test_skills_database.py
```

固定语义用例见 `tests/evaluation/data/skill_mail_cases_v1.json`。对同一输入比较关闭 Skill 与启用续约 Skill：
报告选择命中/误加载、明确要求覆盖、无来源承诺、事实保留、格式符合、调用次数、Token 和耗时。
至少逐例人工核对事实，不能只检查关键词或将工程 Mock 测试称为质量评测。
当前只完成工程验证，尚未发起这组真实模型 A/B 实验，也没有可宣称的质量提升数据。
通过后再考虑协作邮件要求指南；不在本轮给所有专家统一注入。

2026-10-24 验证记录：整体离线回归 Worker 832通过/2跳过，API 178通过/37跳过。
之后补充了选择摘要中保留用户要求的测试，以及真实SQL使用记录/分页/归属测试；
最终定向回归 Skill+记忆调用39通过，API Skill契约+真实PostgreSQL测试5通过。
数据库测试确认临时schema/角色已回滚；没有执行业务库022迁移，没有调用真实模型。
前端目录中已有的独立改动未由本轮修改。
