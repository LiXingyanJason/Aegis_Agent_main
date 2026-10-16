# 权限前置 Hybrid 检索与 RRF（6.4）

## 1. 状态与边界

已实现 PostgreSQL 全文召回、受治理查询 Embedding、Qdrant 只读召回、RRF、SQL二次复核、诊断与显式降级。
生产 Worker 已接入真实实现，默认开关仍关闭；未修改实际 `.env`、业务数据库或云端数据。
复用012／013已有字段和索引，本阶段没有新增迁移，不需安装pgvector。
真实云服务联调、真实语义召回质量及检索效果对比留待显式验收；不以替身测试声称召回率提高。
暂不增加Rerank、新版本管理、知识库MCP服务或新的向量数据库。

## 2. 调用关系

```text
KnowledgeQueryWorkflow → KnowledgeQueryService（步骤／审计／事件）
  → ToolGateway + Harness → KnowledgeSearchHandler
    → HybridRetrievalService
      1. KnowledgeRetrievalRepository.scope：可信tenant/user + 可见范围 + enabled + ready当前版本
      2. SQL模型空间登记核验：版本绑定、集合namespace、模型版本／维度
      3. KnowledgeRetrievalRepository.lexical：同范围内全文召回
      4. EmbeddingExecutor（run/knowledge_query预算、授权、超时、Token审计）
         → EmbeddingClient → QdrantSearch（同版本白名单，仅只读密钥）
      5. ordered_hits / fuse：去重、稳定RRF
      6. KnowledgeRetrievalRepository.load：再次检查SQL事实后读取安全摘录
  → EvidenceService / KnowledgeEvidenceRepository：回答前、生成后再次复核
  → KnowledgeAgent：只依证据回答 → 来源Artifact与现有SSE
```

当前KnowledgeQueryPlan只包含query、最多2条改写和top_k≤5；暂不提供资料类别筛选。
历史资料类别值留在数据库，不参与检索或对外接口。SQL权限与范围上限保持不变。
SQL与网络操作不进入Agent；模型不能指定租户、身份、文档白名单或Qdrant过滤条件。
旧RetrievalService保留为显式单路／测试适配和未配置拒绝入口，不作为生产Hybrid实现。

## 3. 权限前置

SQL范围同时要求：同租户、本人私有或同租户共享、enabled=true、未删除、非敏感、允许外发、
current_version_id匹配且版本ready。查询记录还必须与已成功索引任务的collection／namespace及固定Embedding空间一致。
范围为空直接返回no_accessible_documents，不触发Embedding或Qdrant。
不自动接管未绑定的集合、不自动重建资料。不同模型空间不能混查，需显式处理后再查询。

Qdrant强制tenant_id、SQL版本UUID白名单、model_provider/model/model_version/dimensions过滤。
不依赖可能过期的payload.visibility；不请求正文payload或向量，不使用写密钥，不创建集合。
过滤范围超过200版本时明确KNOWLEDGE_SCOPE_TOO_LARGE，不截断为200条，也不取消权限过滤。
首版不实现无界分批；超出版本范围时明确报错。

网络调用期间可能撤权，因此返回的chunk_id仍需在SQL中复核身份、启用状态、当前版本、indexed_at，
未通过的不读取文本、不进入State。图层生成前及交付前沿用已有来源复核。
这不代表能撤回用户已经看到的内容，也不承诺跨PostgreSQL与Qdrant的原子权限快照。

## 4. 全文、向量与RRF

查询复用indexing/chunker.search_tokens，与索引侧相同：英文／数字词项转小写，中文单字与相邻双字，去重。
query和有限改写的词项合并成带引号的OR tsquery，作为绑定参数传给to_tsquery('simple',...)；
使用search_text的GIN索引和ts_rank_cd，排名相同用chunk UUID打破平局。不是全文LIKE，也不让LLM拼SQL。
中文单字会扩大召回，词面命中不等于语义正确；后续应以固定评测集决定是否改进分词及排序。

查询文本一批（原问题+改写，去重后最多3条）进入EmbeddingExecutor，主体是实际run_id，purpose=knowledge_query。
外发授权开关、真实run归属／running状态、模型空间、持久化预算、超时、Token用量均复用已有治理。
每个查询向量一次Qdrant /points/query，无额外网络重试；params.exact=true用于首版小规模候选检索。
每个查询最多20候选，多查询合并相同chunk取最高Cosine分，再按分数／UUID排序，向量路总候选仍≤20。
适配协议参考[Qdrant Query points官方文档](https://api.qdrant.tech/api-reference/search/query-points)。

两路分别去重并有界后，按 `score(chunk) = Σ 1/(60 + rank)` 融合；某一路未出现不加分。
同分按chunk UUID稳定排序。同一固定候选快照的融合结果稳定，不将RRF值或Cosine解释为正确概率。
最多40个融合候选进入SQL复核，复核后才选top_k并连续编号；原文和hash不裁剪或改写。
结构化结果超过12,000 UTF-8字节时移除末位证据及对应诊断，而不是截断片段；实际返回可少于top_k。

## 5. 策略、错误与降级

默认值集中在retrieval/policy.py的不可变RetrievalPolicy：

| 项 | 默认 |
| --- | --- |
| policy_version | hybrid-v1 |
| 可访问版本上限 | 200（读取201条检测超限） |
| 全文／向量候选上限 | 各20 |
| 查询词项上限 | 128，超过直接拒绝 |
| RRF常量 | 60 |
| 返回结构化字节上限 | 12000 |
| 全文降级 | false |

调整默认额度／排名算法需更新策略版本及回归测试；Embedding治理额度仍由EmbeddingPolicyProvider管理，不能替代或重置。

默认Qdrant故障返回稳定ToolError，不把失败伪装为空证据。
只有显式配置KNOWLEDGE_LEXICAL_FALLBACK_ALLOWED=true，且错误为QDRANT_UNAVAILABLE时，
才丢弃所有向量路的部分结果，返回lexical_only／degraded=true／reason=vector_unavailable。
鉴权失败、集合／过滤拒绝、查询超时、Embedding故障、外发授权不足及预算耗尽不走降级，也不自动修复或重试。
Qdrant Cloud若要求payload索引，需在运维侧配置过滤字段索引；查询拒绝会返回QDRANT_QUERY_REJECTED，
查询进程不会为了通过请求而创建索引或放宽过滤条件。

无证据原因：no_accessible_documents、no_matches、sources_unavailable、result_limit。
degraded的reason优先保留vector_unavailable，即使全文也无结果，不能掩盖服务故障。
其他稳定错误包括KNOWLEDGE_CONTEXT_REQUIRED、KNOWLEDGE_SCOPE_TOO_LARGE、KNOWLEDGE_QUERY_LIMIT、
EMBEDDING_PROFILE_MISMATCH、QDRANT_PROFILE_MISMATCH、QDRANT_NOT_CONFIGURED、QDRANT_AUTH_FAILED、
QDRANT_QUERY_REJECTED、QDRANT_TIMEOUT，以及原有EMBEDDING_*治理／通信错误。

## 6. 结果、State与前端

KnowledgeSearchResult保持evidence，新增可选retrieval：policy_version、mode、degraded、reason、rankings。
每个已交付片段诊断含chunk_id、lexical_rank／vector_rank（未召回则null）、rrf_score；只保留≤5项。
不保存额外问题全文、ACL或向量；诊断不传给回答模型。
State的knowledge_retrieval保留诊断，tool_calls响应及tool_preview.output_summary.retrieval用于审计。
降级成功时，最终回复由代码前置“本次向量检索不可用，以下仅依据关键词检索结果回答”，不让模型隐藏降级。
已有knowledge_sources Artifact不改变，现有前端可忽略新增审计字段；需要展示来源仍按原Artifact契约处理。
不新增浏览器直调knowledge.search的REST接口，来源详情／下载仍未实现。
接口说明追加于根目录《第01期-接口设计.md》篇末第7章，早期5.8占位状态以第7章为准。

## 7. 启动与验收

先确保主后端已按当前head升级（至少包含012／013），上传虚构资料、显式启动索引，等ready后显式启用。
API／Index Worker保持原流程，聊天Worker额外检查下列配置：

```dotenv
KNOWLEDGE_QUERY_ENABLED=true
EMBEDDING_ENABLED=true
EMBEDDING_EXTERNAL_QUERIES_ALLOWED=true
QDRANT_ENABLED=true
KNOWLEDGE_LEXICAL_FALLBACK_ALLOWED=false
```

只在允许将问题发送给外部供应商时设置外发true；并非允许任意敏感业务问题外发。
Embedding模型／版本／维度必须与索引相同，Qdrant URL／collection必须对应原namespace，
配置QDRANT_API_KEY只读密钥；聊天查询不需要QDRANT_WRITE_API_KEY。
重启聊天Worker后在对话框询问“公司续约政策要求提前多久通知？”，无需启动日历／邮件MCP。
未启用、未ready资料不应返回；禁用后下一次查询与来源复核均应排除。

回归使用项目既有完整tests收集（部分旧测试跨目录导入，不宜只执行tests/knowledge目录）：

```powershell
# aegis_agent_worker目录，默认离线
D:\anaconda\envs\Aegis\python.exe -m pytest tests -q -p no:cacheprovider
# Aegis_backend目录，显式数据库测试，仅随机临时schema/角色且全部回滚
$env:AEGIS_KNOWLEDGE_DB_TESTS='1'
D:\anaconda\envs\Aegis\python.exe -m pytest tests -q -p no:cacheprovider
```

2026-10-16：Worker419项通过，API113项通过（包含21项真实PG临时schema测试）。
覆盖RRF重复／同分、空范围、无结果、范围和词项上限、结果字节上限、模型空间不匹配、故障／超时／降级、
真实PG中文／邮箱／编号／中英混合召回、私有／共享／跨租户、绕过RLS角色仍受代码过滤、禁用／敏感／撤权／非当前版本，
以及真实Embedding预算／逐次审计SQL和向量返回后撤权的丢弃。
语义改写走向量路的样例使用确定性向量替身，不能作为真实供应商语义质量的结论。
所有临时对象回滚，不升级业务库、不读写远端集群，不更改实际.env。

## 8. Qdrant过滤索引初始化修复（2026-10-16）

Qdrant Cloud可能要求过滤字段已有payload索引；向量已写入不代表可执行带权限过滤的查询。
本次实际故障为`QDRANT_QUERY_REJECTED`，HTTP 400提示`Index required but not found`，集合原payload_schema为空。

`vector/qdrant_index.py`的`ensure_collection`现在在维度／距离校验通过后，调用`ensure_payload_indexes`：

| 字段 | 新建类型 |
| --- | --- |
| tenant_id、version_id、document_id | keyword |
| model_provider、model、model_version | keyword |
| dimensions | integer |

前六类查询字段覆盖强制过滤，document_id用于按文档清理；不为正文建立索引。
已有ID字段的uuid类型兼容并保留；其他类型冲突明确失败，不删除重建。
仅补建缺失项，使用写密钥请求`PUT /collections/{collection}/index?wait=true`，检查completed并重新读取集合确认。
失败／未确认不继续宣称索引就绪；重试时跳过已存在且兼容的索引。
新集合与旧集合均走此检查；聊天查询端仍只读，不自动取得写权限、不放宽tenant／version过滤。

已有ready资料无需重新上传或生成Embedding：维护时可用配置的写密钥执行
`QdrantVectorIndex(settings).ensure_collection(EmbeddingProfile(...))`（异步调用，使用当前固定模型配置），
只补索引、不改动points。后续Index Worker需重启加载新代码；普通查询不依赖Index Worker持续运行。

本次已为当前配置集合补齐7个索引，points_count修复前后均为2；读取密钥下带完整过滤的诊断请求成功。
该诊断使用不存在的身份／版本和零向量，预期返回0条，只验证协议与过滤可用，不代表语义回答质量已验收。
没有执行SQL迁移、向量重写或Embedding调用。API／SSE契约不变，前端无需配合。
修复后Worker完整离线回归461项通过，覆盖新旧集合补建、重复执行不写入、UUID索引兼容、类型冲突、服务失败及未确认结果。

接口依据：[Qdrant Create payload index](https://api.qdrant.tech/api-reference/indexes/create-field-index)。
