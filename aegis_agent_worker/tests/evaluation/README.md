# 固定主路由评测（4.6，第一步）

当前已编写 62 条固定用例及运行／对比脚本。此阶段准备评测工具，不代表真实模型评测或上线验证完成。

## 范围与安全边界

- 使用生产 MainAgent、MainDecisionWorkflow、GuardedLLMClient 和 HarnessExecutor；仅把数据库预算仓库及业务事实换成内存实现。
- 领域工具不执行，卡片读取使用固定快照并经过 Harness 工具预算；身份、失效、版本错误由快照替身返回。不是真实 RLS／ToolGateway 集成测试。
- 4 条页面对照用例经过生产根图，领域子图是替身，必须绕过主分类；审批和补充问答恢复仍由现有回归测试覆盖，本评测集尚未进行真实恢复联调。
- Mock 场景的响应由 `mock` 字段显式固定，与 `allowed` 分开维护。它只测试解析／校验／路由程序，不证明 LLM 理解用户。
- 默认 HTTP Mock，模型使用占位配置，不读取 `.env`；不启动 API、Redis、PostgreSQL 或 MCP，不写业务表。
- live 只有 semantic 场景调用真实模型；fault 故障注入和 page 对照永远离线。真实模式需显式授权开关，整轮请求上限控制实际 HTTP 请求数，次数不是金额上限。

## 用例格式与人工复核

`data/main_routing_cases.json` 固定当前时间为 2026-10-14 10:00 北京时间，包含：

- 直接表达、同义问法、邮件摘要／起草只导航卡片；
- 普通沟通、歧义、跨领域任务、未知能力、提示注入；
- 有效、缺失、过期、版本变化及其他租户卡片；
- 非法模型输出、有界修复、网络失败、伪造目标与批准；
- 页面摘要、起草、发送确认及草稿修改入口。

`allowed` 是预先人工拟定的可接受决策集合，不比较回复措辞；`target=valid` 要求引用固定邮件 ID 和 v1。
模型查询完整主题允许无 target。`forbid_delegation` 声明该安全用例不允许交接任何领域子图。
这些初始标签应先人工复核，再冻结版本；不要观察某次模型输出后修改期望以提高成绩。
改标签／快照要升级 dataset version，报告记录文件 SHA256；历史报告不能覆盖。

## 离线运行

在 `aegis_agent_worker` 目录：

```powershell
D:\anaconda\envs\Aegis\python.exe scripts/evaluate_main_routing.py --output evaluation-results/mock-v1.json
D:\anaconda\envs\Aegis\python.exe -m pytest tests/evaluation -q -p no:cacheprovider
```

`--case-id card01` 可选择单条，`--limit 5` 可冒烟测试，`--repeat 3` 可重复运行。
默认每轮最多 200 次 HTTP 请求；耗尽时报告不完整／受限错误，不能当完整结果对比。
Mock 报告 `semantic_routing_accuracy=null`，Token 缺失也为 null，绝不把 Mock 100% 当模型准确率。

## 显式启用真实模型（须授权，历史结果见下文）

本地 Worker `.env` 放模型配置，不把 Key 发到聊天。脚本使用已有 Settings，数据库／Redis配置仅解析而不连接。
初次建议只跑 5 条，确认配置、费用和报告后再扩展：

```powershell
D:\anaconda\envs\Aegis\python.exe scripts/evaluate_main_routing.py --mode live --allow-live --env-file .env --limit 5 --max-model-calls 15 --output evaluation-results/live-smoke.json
```

只有 live 的 semantic 子集参与真实路由正确率；故障注入、页面对照另外看逐条结果，不能混入模型准确率分母。
没有原始输出、提示词或 Key 写入报告；记录安全 ID、决策分类、目标校验布尔值、错误类型、次数和用量。

## 新旧版本对比

建议基线 `ed91bfb`，增强版 `400c353`（或之后已固定的 commit）。准备两个独立源码目录，不切换正在开发的工作目录。
`--worker-source` 指向包含 `src/aegis_agent_worker` 的 Worker 目录；脚本在新进程优先导入该源码，不自动 checkout 或创建 worktree。
始终使用当前这份脚本和同一数据集，只改变被测源码路径；旧版适配预取引用接口，新版适配受控卡片工具。
旧版没有候选筛选时视为完整能力目录，并在报告明确标记 `legacy_full_catalog`。
共同 HTTP 包装器读取供应商 usage，不需要修改旧版生产代码；旧版用途标签未知时保持 null。

示意命令（替换源码路径）：

```powershell
D:\anaconda\envs\Aegis\python.exe scripts/evaluate_main_routing.py --worker-source D:\eval\before\aegis_agent_worker --label ed91bfb --output evaluation-results/before-mock.json
D:\anaconda\envs\Aegis\python.exe scripts/evaluate_main_routing.py --worker-source D:\eval\after\aegis_agent_worker --label 400c353 --output evaluation-results/after-mock.json
D:\anaconda\envs\Aegis\python.exe scripts/compare_routing_reports.py evaluation-results/before-mock.json evaluation-results/after-mock.json
```

真实对比时两次都添加 live 授权及相同 env-file，固定模型名称／配置、用例范围、重复次数及快照。
脚本校验数据集 hash、固定时间、模式、用例／重复范围、模型名称及完整性。
报告附 commit、源码 hash、源码是否有未提交改动；供应商服务端版本／负载变化仍需人工记录。
已实际运行首轮旧新版对比，结果见 [2026-10-14实验报告](../../文档/阶段1/09-主决策路由评测实验报告-2026-10-14.md)。
历史源码可加 `--source-revision ed91bfb`，脚本按Git blob核验所有Python源码（允许CRLF行尾等价），
避免导出目录报告成父仓库HEAD。源码不匹配时拒绝运行，不能用标签替代版本核验。
对比脚本支持 `--output` 保存比较JSON，不覆盖历史文件。

## 指标解释与尚未完成的工作

- 候选召回率：允许的领域能力至少一个被候选保留；另看候选数，旧版全目录召回高不代表更好。
- 澄清率：仅 semantic 子集；不通过期望且澄清的用例计入错误澄清数，澄清不天然是失败。
- 平均／P95：整个决策流程含辅助卡片读取、修复及校验，不是仅模型网络耗时；P95 使用 nearest-rank。
- Token：报告每种计数的已报告总量、已知和未知调用次数，不推算缺失值或金额。
- `prohibited_handoff_rate`：违反用例禁止交接规则的比例；不是生产非法外部调用率。
- `illegal_call_rate=null`：沙箱不执行业务工具，不能将预设零调用当成权限／审批安全证明。
- `ready_for_release=false`：始终不能仅凭这份路由报告声明上线通过。

历史单轮新旧对比及本次三轮结果均已记录；后续仍需人工复核标签、独立留出集、同条件多轮基线与完整权限／恢复场景的端到端联调，不能仅凭本集结果声明生产验收完成。
代表性正确率≥90%只是目标；关键非法写入／审批绕过为0需在真实治理链及端到端测试中验证。

## 2026-10-22：简历量化实验与三轮重复评测

已新增[实验设计、实测结果及可用简历表述](../../文档/阶段1/24-简历量化实验设计与记录-2026-10-22.md)。同一53题×3轮，真实语义152/159合格（95.60%）；重复观测不是159个独立问题，不能混入故障Mock/页面对照或只挑最高一轮。保留7次失败，不据此修改标签。没有同期基线，不能宣称提升百分比。

scripts/summarize_resume_experiments.py可统计逐轮/逐题一致性、按题聚类bootstrap区间、只包含live请求的供应商usage、JUnit模块覆盖，并用--evidence-directory归档显式输入到非忽略目录；拒绝覆盖历史证据。模型外发/费用需另外授权，汇总脚本本身不调用模型/数据库/MCP。统计/归档逻辑有9项离线测试。

本轮证据已归档于文档/阶段1/实验数据/20261022-routing，不依赖evaluation-results中的临时文件。新的RAG、记忆和Schema消融仍为设计，不把既有单测当成召回率或成本提升。

## E04真实Hybrid消融（2026-10-22）

后续已单独授权并完成E04：[预注册方案、结果与限制](../../文档/阶段1/25-Hybrid检索消融实验-E04-2026-10-22.md)。scripts/evaluate_hybrid_retrieval.py使用冻结虚构语料、真实PostgreSQL SQL/阿里云Embedding治理/Qdrant/RRF，仅建隔离schema/collection；不可未经授权运行。无聊天模型、真实业务资料或原始Embedding响应入报告。

scripts/retrieval_eval提供qrels、Recall/MRR/Hit/nDCG和按文档聚类的配对区间；summarize_hybrid_retrieval.py负责只读归档，核验断言复核未改变原始排名/标注/参数。verify_hybrid_e04_report.py仅处理本次旧报告的一项片段断言过严，不是自动重试/绕过其他失败的通道；原资源已清理，不能再次复核那个旧run。

证据位于文档/阶段1/实验数据/20261022-e04：36条测试查询上RRF Recall@5=98.61%，全文90.28%，纯向量100%；不能只报告对全文的改善而隐瞒纯向量更优。6条无答案均返回候选，不是拒答验证；权限验证是实际SQL谓词，不是完整角色RLS。E04新增13项本地测试，Worker813通过/2跳过；不把测试数当模型检索正确率。
