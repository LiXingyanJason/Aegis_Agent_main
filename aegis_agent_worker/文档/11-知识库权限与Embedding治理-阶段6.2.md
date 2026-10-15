# 知识库权限、Embedding 与 Qdrant Cloud（6.2）

## 1. 当前状态与存储选择

本页说明6.2边界；后续6.3已实现本机源文件导入/索引，新增012_knowledge_indexing与013_knowledge_lifecycle。
当前代码head为013；上传、索引、启用分别操作，新旧资料均默认禁用。本次未升级业务库；见[6.3部署与接口](12-知识资料导入与索引-阶段6.3.md)。
真实Hybrid检索/OSS仍待后续步骤，不将配置完成当作查询成功。

向量路线已改为 **本地 PostgreSQL + Qdrant Cloud 免费集群**；无需迁移原数据库或安装 pgvector。

- PostgreSQL：文档元数据、版本、片段、权限、全文索引、索引任务、Embedding预算/审计；当前版本表还保存source_text。
- Qdrant：后续保存向量和稳定引用/过滤元数据，作为可重建索引，不是权限事实源。
- Redis：后续投递索引任务；现有聊天/审批链路不变。
- Embedding：继续使用独立供应商配置和治理，不因采用免费向量库而自动免费。

本地库已核验为 `011_embedding_governance`。010/011保持原样；
未提交且未执行的 `012_knowledge_pgvector.py` 已移除；6.2时head为011，后续追加012／013，不改写历史迁移。
**没有执行数据库回滚、stamp、云集群创建、远端写入或资料上传。**
6.3导入与Qdrant写入适配已实现，真实Hybrid检索仍待6.4；配置开关不等于查询链路已接入。

### 源文件存储分工（6.3已实现本机适配，OSS待扩展）

| 存储 | 保存内容 |
| --- | --- |
| 本机受控目录（开发）／OSS（后续） | 不可变源文件及其历史版本 |
| PostgreSQL | 元数据、权限、版本、受控storage_key、解析后的片段及索引任务 |
| Qdrant | 片段向量和引用/过滤元数据，不保存源文件或片段正文 |

引入DocumentStorage接口，先实现LocalDocumentStorage，后续替换为OSS适配。
数据库保存存储类型、key、大小、MIME和源文件hash；临时下载URL按权限校验后生成，不持久化。
源文件hash与解析文本/片段hash分开；存储key不可变，不覆盖有效源文件。
完整内容更新、多版本切换、历史恢复/追溯暂缓；首期只实现新资料导入和同一任务重试。
API/Worker在开发时使用同一根目录或共享卷；本地key禁止路径穿越或符号链接逃逸。
文件存储与SQL没有共同事务，导入失败的孤立对象须可清理；SQL撤权立即生效，不等待文件清理。

010的source_text原为必填，012新增文件引用并允许该字段为空；旧内联记录继续读取，
不修改010/011历史迁移。部署新版代码前须升级到013，详细实施及验收见开发计划6.3。

## 2. 表与权限

| revision | 内容 |
| --- | --- |
| 010_knowledge_schema | documents / document_versions / chunks / index_jobs、复合关系约束、全文GIN索引、RLS |
| 011_embedding_governance | embedding_execution_budgets / embedding_execution_attempts |

```text
knowledge_documents（归属、范围、分类、删除状态、外发授权）
  └─ knowledge_document_versions（原文/hash、状态、固定模型/版本/维度）
       ├─ knowledge_chunks（片段、位置、hash、全文索引，不存向量）
       └─ knowledge_index_jobs（索引状态及重试次数）
```

版本/片段复合外键防止不同文档、租户、模型空间混用；current_version_id限定本文件版本。
默认private仅本人读，tenant仅同租户共享；共享读者不能修改资料或创建所有者索引任务。
标签只分类，不声明ACL。KnowledgeService.set_visibility只根据认证所有者修改范围，暂无HTTP入口。
知识表启用且FORCE RLS；Repository仍显式限定tenant/user、范围、删除状态。
KnowledgeEvidenceRepository只返回当前ready版本且仍与旧引用完全一致的可访问片段。

本地PG16.15运行账号为超级用户/BYPASSRLS，生产必须改用受限账号；FORCE RLS不能约束超级用户。
身份只能由tenant_transaction注入，不接收模型指定租户/用户。
索引外发需本人任务、资料未删除、非敏感、external_embedding_allowed=true且版本模型匹配；
本版敏感资料直接拒绝，分享给同租户不等于批准发送到第三方。云向量/元数据也属于外发数据。

## 3. 云集群准备（用户操作）

1. 在[Qdrant Cloud](https://cloud.qdrant.io/)创建Free集群，按控制台实际可选区域选择。
2. 复制集群HTTPS Endpoint，不是控制台页面URL。
3. 创建 **Database API Key**，不是Cloud Management Key。
   查询建议只读，索引消费者使用写密钥；密钥只存Worker环境，不能给前端/LLM或写入Git。
4. 将统一 `.env.example` 中Qdrant和Embedding设置合并到实际 `.env`。
5. 配置完成后，待6.3接入客户端、连通性检查及索引，再做虚构资料联调。

Qdrant写入客户端已接入独立Index Worker；仅配置true不会立即请求，启动并消费导入任务才会初始化collection/写入向量。
查询适配仍待6.4，不会自动开放知识对话。
启用配置时要求HTTPS及非空读取密钥，索引写密钥在后续写入入口检查。
collection模板名不代表已经创建；后续将固定模型/版本/维度映射到collection，
遇到不匹配必须拒绝，不能自动删库重建。

```dotenv
QDRANT_ENABLED=false
QDRANT_URL=
QDRANT_API_KEY=
QDRANT_WRITE_API_KEY=
QDRANT_COLLECTION=aegis_knowledge_v1
QDRANT_REQUEST_TIMEOUT_SECONDS=10
```

当前官方Free规格为0.5vCPU、1GB RAM、4GB磁盘、单节点；区域有限，仅手动快照API，
闲置1周暂停，未重新激活且4周不活动可能删除。不能作为唯一不可恢复的数据存储。
上线前重新核对[官方限制](https://qdrant.tech/documentation/cloud/create-cluster/)；
密钥权限参见[认证文档](https://qdrant.tech/documentation/cloud/authentication/)。
首期只用虚构资料，实际容量/延迟必须测量，免费集群不承诺本项目生产SLA。
目前尚未连接任何用户集群验证网络或容量。

## 4. Embedding分层、配置及治理

Embedding已独立成包，聊天目录不承担向量化通信：

```text
embedding/
├── client.py          # 供应商通信、响应数量/维度/数值校验
├── contracts.py       # EmbeddingProfile
├── exceptions.py      # EmbeddingError
└── bootstrap.py       # create_embedding_executor组合入口
common/usage.py        # 共享TokenUsage，不依赖聊天目录
harness/
├── embedding_executor.py
└── policies/embedding.py
repository/embedding_repository.py
```

`llm/usage.py`仅保留聊天用途上下文；LLM客户端与Embedding客户端都从common导入TokenUsage。
这次只调整目录和依赖边界，不改变配置、执行策略、调用协议或数据库结构。

```text
后续索引/查询入口
  → create_embedding_executor(settings, database)
  → EmbeddingExecutor.embed(EmbeddingSubject, texts)
  → EmbeddingPolicyProvider + EmbeddingRepository
     数据/身份检查 → 固定策略和模型 → 原子预算及审计
  → 事务外：共享execution_timeout → EmbeddingClient → /embeddings
  → 校验数量/顺序/维度/有限数值 → 成功或失败审计
```

业务必须用治理执行器，不直接调裸客户端；复用租户事务、超时组件、稳定错误和Tracing，
预算采用专属表/run与job主体，不伪装聊天任务。不自动重试网络调用，失败也消耗占用额度。
源文/向量不写入调用审计；prompt/total tokens缺失为NULL，不冒充0、不计算未经核实金额。

配置：EMBEDDING_ENABLED默认false；启用需独立PROVIDER、API_BASE、API_KEY、MODEL及MODEL_VERSION。
DIMENSIONS默认1536、范围1～2000，这是当前表契约限制，不是Qdrant自身限制；
必须匹配真实供应商输出，变更需新文档版本/collection及重建索引，不可混用旧向量。
SEND_DIMENSIONS默认false，仅供应商支持时开启；REQUEST_TIMEOUT_SECONDS默认30。
EXTERNAL_QUERIES_ALLOWED默认false，查询文本外发需授权。不能把聊天complete_json输出当作向量。
当前策略额度不会因更换向量库而调整：

| 用途/主体 | 策略版本 | 次数 | 累计字符 | 单批条数/字符 | 超时 |
| --- | --- | --- | --- | --- | --- |
| knowledge_index/job_id | embedding-index-v1 | 128 | 2,000,000 | 16 / 16,000 | 30秒 |
| knowledge_query/run_id | embedding-query-v1 | 4 | 8,000 | 3 / 2,000 | 15秒 |

首次绑定策略及provider/model/version/dimensions；恢复不重置额度或替换模型空间。
这是字符/次数预算，不是Token计费预算。具体字段在harness/policies/embedding.py。
索引复建需要显式新任务及治理规则，不能偷偷清空原任务预算。

## 5. 6.3/6.4实施要求

优先打通上传 → 索引任务 → 解析切块 → Embedding → Qdrant → 检索。
现有version_id/hash/模型空间保留作为技术关联；ready后首次绑定current_version_id，
不要求多版本更新、切换或历史管理。删除、失效、撤权和索引一致性仍是硬性验收项。

- 新增VectorIndex协议和Qdrant适配，不把Qdrant SDK/SQL写入KnowledgeAgent。
- 源文件拟存本机/OSS，解析片段及权限在SQL；Qdrant保存向量、chunk_id/document_id/version_id、模型与必要过滤元数据。
- 幂等point_id=chunk_id；写入确认后才标ready并首次绑定SQL内容记录，不实现多版本切换。
- SQL与Qdrant无共同事务，使用任务状态、幂等批次重试和补偿清理，不标记半成品为成功。
- 检索先确定SQL可信可见范围，Qdrant强制tenant及授权当前版本过滤；范围过大有界分批/拒绝。
- 返回候选必须在SQL再次核验后读取文本，绝不把滞后payload ACL当授权证明。
- 全文与向量排名由RetrievalService进行RRF；远端故障只按显式策略降级，并标记单路。
- 保留源数据、模型信息及索引任务用于重建；云集群失效不影响SQL中权限/版本事实。
- 不默认启用Qdrant Cloud自动推理；采用该服务需另行适配预算/模型/外发规则。
- 前端本阶段没有新增必调接口；导入/资料管理/来源详情按后续阶段同步。

## 6. 迁移与测试

在Aegis_backend中运行 `python -m alembic current`、`python -m alembic heads`，
两者应为011。当前库已完成升级，不需要重复处理或stamp。
未来迁移追加新revision，不改写已执行的010/011。降级会删除知识/预算数据，先停代码并备份。

已验证：独立包调整后Worker离线374项、真实PG迁移/权限测试7项通过；主后端此前全量74项通过。
新增测试约束Embedding包不直接依赖聊天模块，并确保两个客户端复用同一TokenUsage契约。
覆盖迁移升降级、私有/共享/跨租户、撤权、SQL预算/模型绑定、敏感拒绝、外发限制及配置校验。
未测试真实Qdrant API、云网络/索引性能或Embedding供应商；不能据此宣称检索已接入。

真实DB测试在开发/测试库运行：

```powershell
$env:AEGIS_KNOWLEDGE_DB_TESTS='1'
D:\anaconda\envs\Aegis\python.exe -m pytest tests/migrations/test_knowledge_database.py -q -p no:cacheprovider
Remove-Item Env:\AEGIS_KNOWLEDGE_DB_TESTS
```

随机schema/受限角色在事务中全部回滚，不修改现有业务表；需测试管理员有创建权限。
Worker的 `python scripts/check_knowledge_database.py` 为只读本地DB检查，不访问云服务。
