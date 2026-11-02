# 3.6 Qwen Rerank 可选接入与冻结消融

日期：2026-11-02。状态：可选工程链路、预算迁移、资料许可、HTTP／检索测试及真实实验脚本已实现；默认关闭。真实供应商调用及质量消融未执行，不声明3.6质量／上线整体验收完成，也未在业务库运行迁移。

## 1. 协议核实与配置

实现采用专用DashScope text-rerank HTTP协议，不经过聊天complete_json。输入`model`、`input.query/documents`、`parameters.top_n`，读取`output.results[index,relevance_score]`及供应商usage。请求全部候选返回完整排名。2026-11-02按用户指定将默认模型更新为 **Qwen-Rerank（qwen3.7-text-rerank）**；新模型省略旧可选return_documents参数，仅消费索引和分数。显式配置旧qwen3-rerank时保留原return_documents=false请求兼容。文档依据：[用户指定的模型页面](https://www.qianwenai.com/models/qwen3.7-text-rerank)、[官方Text Rerank API](https://help.aliyun.com/zh/model-studio/text-rerank-api)，本次只查阅文档，没有向供应商发送资料。

本次重新核实，模型页面提供千问平台北京端点，百炼官方也给出北京工作空间qwen3.7-text-rerank调用示例，撤销此前“仅新加坡可用”的说明。端点与密钥须来自匹配平台／工作空间，不能仅凭已有Embedding授权判断可以调用。默认切换不自动开启外发；显式旧模型配置不会被覆盖。当前允许qwen3.7-text-rerank及旧qwen3-rerank，其他模型仍拒绝。

Worker模板新增：

```dotenv
RERANK_ENABLED=false
RERANK_API_URL=https://cn-beijing.maas.qianwenaiapi.com/api/v1/services/rerank/text-rerank/text-rerank
RERANK_API_KEY=
RERANK_MODEL=qwen3.7-text-rerank
RERANK_EXTERNAL_ALLOWED=false
RERANK_FALLBACK_ALLOWED=false
```

RERANK_API_URL是完整HTTPS接口，不是Embedding的`/compatible-mode/v1`。模板使用模型页面的千问平台端点；若使用百炼工作空间，改为`https://<工作空间ID>.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank`并使用匹配的密钥。密钥独立配置，不读取Embedding密钥，不打印凭据，不自动跟随重定向。已有.env中如显式设置旧RERANK_MODEL，需主动改成qwen3.7-text-rerank并重启Worker。本次不改本机.env、不启用收费调用。

模型更换不改变向量内容，无需重建Embedding索引或新增数据库迁移。预算、候选数／字符上限、超时、资料许可和严格／降级语义不放宽；审计和live实验报告仍记录实际model名。qwen3.7返回的prompt_tokens／total_tokens复用现有usage解析，未返回的completion_tokens保持unknown。模型不固定在既有任务快照，切换前应处理完在途重排任务，避免同一任务恢复时混用模型。原模型实验记录不得重标为新模型结果，新模型质量需重新评测。

## 2. 分层与调用链

- `rerank/client.py`：专用HTTP、流式响应上限、索引／数量／有限分数校验、错误分类及供应商usage。
- `rerank/executor.py`：可信run身份、用途策略、输入上限、外发检查、Harness逐次审计与超时；不加载长期记忆或Skill。
- `harness/policies/rerank.py`：局部固定额度；默认20候选、问题1000字符、单条800、总17000字符、响应64000字节、15秒。字符限制不是Token计费。
- `HybridRetrievalService`：RRF融合 → SQL读取授权候选 → 最多20条原文摘录重排 → 再次SQL复核 → 最终top_k及原结果额度。生产B扩展仍未上线，因此没有将离线多范围扩展器直接拼到本链路。
- `KnowledgeRetrievalRepository.rerank_authorized`：请求发出前再次执行既有VISIBLE＋独立external_rerank_allowed，比较本批chunk／version／hash；并不把Embedding许可当重排许可。

只对非空的有限候选发一次请求，不自动重试。响应必须完整包含每个候选一次；重复／越界／bool索引、缺失项、非数值／NaN／超范围分数均拒绝。按分数降序，同分按原候选顺序稳定排序，不接受供应商返回的新正文作为证据。重排不能证明事实正确、不能召回原本没有进入候选的资料。

新检索诊断含rerank_applied、rerank_policy_version、rerank_reason；最终证据排名保留原始rrf_rank、rrf_score、两路rank及rerank_score。未启用的旧序列化结果不额外增加这些默认字段，保持既有审计兼容。重排期间来源变化的候选不交付；后续知识子图仍执行答案及交付前来源复核。

## 3. 持久化治理和任务恢复

迁移024_rerank_governance（依赖023）新增：

- run_execution_budgets.rerank_calls；原max_models不增加。
- run_execution_attempts.kind允许rerank，新增安全rerank_metadata。
- knowledge_documents.external_rerank_allowed，所有已有资料默认false。

每次`HarnessExecutor.attempt('rerank', ...)`在同一原子SQL内同时增加rerank_calls和model_calls；受专属每run最多2次、原模型总次数、任务running状态、policy_version和累计活跃耗时约束。次数、超时与审计仍复用现有Harness，不新建可重置账户。审计purpose为knowledge_rerank，不冒充聊天生成。

新任务固定策略后缀`-rr1`（严格）或`-rr1f`（显式供应商故障降级）。旧版本仍注册，不自动扩权；恢复保持原额度／模式，不重新选择策略。关闭运行配置或外发开关是显式阻止后续重排，不偷偷跳过。未完成rr策略任务时需保留同版Worker及迁移；不要在有在途任务时改端点／地域／模型配置，当前模型及端点由运维配置提供，不写入数据库任务快照。

审计记录候选ID／版本／hash、数量、输入字符、用途、模型、状态、耗时及供应商Token。没有全文／Prompt；usage缺失null，不按字符推计费。授权发生在调用前复核，但无法追溯撤回已发出的请求，存在与其他外发相同的复核后并发撤权窗口。

严格模式失败停止；明确开启降级时，仅RERANK_UNAVAILABLE／TIMEOUT／RATE_LIMITED／RESPONSE_INVALID保留原RRF，并记录未应用及原因。Harness预算／任务取消／权限／输入限额等治理错误不降级、不换供应商、不格式重试。HTTP超时与Harness整体超时分别分类，后者不属于可降级供应商故障。

## 4. 资料外发许可与部署

API新增所有者接口：`PATCH /knowledge/documents/{id}/rerank-permission`，严格JSON `{"allowed":true|false}`。API前缀沿用项目配置。身份由认证依赖提供，不能传tenant/user。资料有权读取不自动允许外发，跨用户共享资料只能由资料所有者授予该许可。撤回后下一次重排请求拒绝，不等缓存／向量清理。

顺序：评审／备份 → 先API目录执行`python -m alembic upgrade 024_rerank_governance` → 部署兼容API／Worker → 核实地域和模型配置 → 所有者明确同意资料外发 → 外发全局开关 → 新任务小范围验证。没有修改用户现有.env，没有替用户授权资料，也没有执行此业务迁移。

降级优先回退应用／关闭新任务重排，不删除审计。存在rerank调用、专属次数或已授权资料时，迁移降级拒绝，须单独制定保留历史方案。前端本次不修改，接口及诊断说明追加到主接口文档篇末；前端不能据诊断触发额外工具调用。

## 5. 冻结消融及真实实验入口

新增`scripts/evaluate_rerank_ablation.py`及冻结文件`实验数据/20261102-rerank-frozen-e04-v1.json`。

离线prepare：从原E04已归档的真实全文／向量候选重现RRF，固定最多20个候选及虚构原文，不只重排最终5条。54条查询=48可答＋6无答案；原36条测试可答题保留，全文／纯向量／原RRF排名保留对照；安全canary不外发，仅允许普通D资料和当前有效的C06.current占位文本。记录历史报告hash，数据未按重排结果修改。

```powershell
# Worker目录；仅冻结，不联网，需使用新输出路径
python scripts/evaluate_rerank_ablation.py --output evaluation-results/rerank-candidates-new.json
# 以下为真实收费实验，仅另行授权后执行；本次未运行
python scripts/evaluate_rerank_ablation.py --live --allow-external --allow-isolated-schema --output evaluation-results/rerank-live-new.json
```

live脚本最多54次请求、无重试，使用随机本地PostgreSQL schema克隆预算／审计结构，真实Harness记录；不创建业务任务、不读取业务资料、不连接Qdrant。虚构快照的外发授权函数不等于生产SQL权限验证，报告须保留这一限制。结果逐题记录排名、分数、质量、run审计和测试集配对区间，保留无答案候选，不把排序模型当拒答器。

同一候选集对比截断RRF与Rerank，另保留历史全文／向量／RRF top5指标；历史纯向量结果不可隐瞒。该短小E04数据不是阶段二长文切分优化验收，也没有验证新的A/B组合。成功先排他保存原始报告再仅删除自己随机schema，并写独立cleanup回执；失败保留准确schema名称和报告供排查，不销毁未归档证据。报告cleanup=false表示写报告时尚未清理，需看单独cleanup回执。

目前仅完成prepare和工程测试，无真实Rerank分数、质量提升、价格或线上耗时结论；后续须另行授权问题／候选原文外发、地域和费用，并核对模型可用性，再运行live。最终上线还需在开发集设置质量／成本门槛和独立测试，不按历史测试结果反复选参。原3.4结构候选的已知退化不会因接入重排自动解决。

## 6. 验证记录

- 完整Worker离线回归933通过／2跳过；首次回归932通过＋1项失败，原因是旧检索诊断序列化新增默认字段破坏原审计精确相等，补充仅在实际出现时序列化新字段后全量通过。
- 随后新增冻结候选及响应限制测试、严格／降级策略配置后，定向34项通过；包含HTTP替身、空／异常响应、索引、同分、使用量、额度／用途、撤权不交付与非治理降级。
- API许可契约3项通过。
- 024实际SQL本地临时schema／受限角色测试通过：专属＋总模型计数、取消阻止调用、attempt及usage落库、默认资料许可false、非所有者／禁用拒绝、历史存在拒绝降级。初次测试适配器缺one()失败，补齐测试适配器后重验通过；不是预算SQL失败。
- 临时schema／角色事务回滚并确认不存在；未修改业务数据。HTTP替身不代表真实供应商可用性或语义质量，测试未包含生产重排网络压测。

最终回归：Worker **936通过／2跳过**，API **181通过／39跳过**；真实临时SQL测试另外显式执行并通过，不把默认跳过的数据库测试计入已通过项。唯一Worker警告为依赖LangGraph的既有反序列化默认值弃用提示。

2026-11-02模型更新回归：Worker **945通过／2跳过**，包含qwen3.7默认配置／开关、两模型白名单、各自请求参数、prompt_tokens与total_tokens及既有预算／权限／故障处理。HTTP使用替身，未实际调用收费模型；原实验结果不改写，不宣称新模型质量提升。
