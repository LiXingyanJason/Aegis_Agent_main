# 会话工作记忆：Redis有界缓存（7.2）

## 1. 实现范围

首版是**确定性会话工作索引**：最近任务的运行ID/类型/状态，以及最近卡片的ID/类型/版本/到期时间。
不调用LLM生成历史摘要，不把消息全文、邮箱正文、知识片段、凭据、卡片payload或未经确认偏好写入Redis。
它不是长期记忆，也不创建`MemoryRecord`、确认偏好或`MemoryUsage`。
近期消息始终从PostgreSQL读取，Checkpoint/Artifact/消息仍是持久事实。

## 2. 调用链

```text
TaskGraph.load_context
  → ConversationContextService.load_history
      → AgentRunRepository：读取近期消息（PostgreSQL）
      → SessionMemoryService.load
          → RedisSessionMemoryCache.read：取缓存及用户代际
          → SessionMemoryRepository.snapshot：按当前run/tenant/user/会话核验SQL快照
          → 内容/序号/版本一致：命中；否则重建有界索引
          → RedisSessionMemoryCache.put：Lua CAS＋TTL＋容量淘汰
      → 有界近期消息＋低优先级JSON工作索引（仅运行时，不写回消息表）
  → 主决策/普通回复读取上下文
      → 需要卡片内容时仍调用既有conversation.cards.list/get
      → 目标归属/来源版本仍由既有Service/ToolGateway复核
```

工作索引使用`context_kind=session_work_index`标识，不能被根图当成最新用户消息。
它只是数据，不证明邮件已发出、日历已写入或用户已经批准操作。
邮件页面后台任务仍直达领域子图，不读取此对话工作缓存；审批/问答恢复仍使用原Checkpoint机制。

## 3. 代码分层

| 文件 | 职责 |
| --- | --- |
| `harness/policies/session_memory.py` | 独立`session-work-v1`额度，经`PolicyProvider.current().session_memory`获取 |
| `repository/session_memory_repository.py` | 单条SQL取同一快照、身份过滤、有界结构字段 |
| `storage/session_memory_cache.py` | Redis协议、超时、CAS、代际及淘汰 |
| `service/memory/session_memory_service.py` | 事实复核、重建、格式化与安全降级 |
| `service/runtime/conversation_context_service.py` | 近期消息装配，插入辅助索引数据 |
| `worker/agent_worker.py` | 受配置开关控制的依赖组装 |
| `Aegis_backend/app/controller/session_memory_controller.py` | 当前用户缓存清理接口 |
| `aegis_frontend/src/store/auth.js` | OIDC退出前清理；故障时仍退出 |

API不导入Worker包，二者仅共享Redis v1协议。跨项目测试检查失效脚本一致。

## 4. 缓存与一致性

key包含`tenant_id/user_id/conversation_id`，Redis Cluster hash-tag按tenant/user隔离。

```text
aegis:session-memory:v1:{tenant_id:user_id}:conversation_id
aegis:session-memory:v1:{tenant_id:user_id}:keys
aegis:session-memory:v1:{tenant_id:user_id}:epoch
```

默认TTL1800秒，每用户最多32个缓存会话，最近6个任务、4个卡片，完整缓存JSON最多12000字节。
索引注册表使用ZSET且有TTL；淘汰同时删除对应缓存键。
epoch只有一个小整数/用户，不含正文且不设置TTL，以免重置后旧写入发生ABA；其数量随用户数而非会话轮次增长。

数据库返回消息最大序号、数据库statement_timestamp微秒序号及结构事实。
Lua拒绝旧消息序号或旧快照覆盖新缓存，不采用各Worker本机时钟决定新旧。
清理时原子推进epoch并删除注册的缓存键；清理前已开始读取的任务携带旧epoch，写回被拒绝。
多浏览器共用账号时，退出一个窗口会清理该用户所有工作缓存，不删除任何持久资料。

每次使用前仍查询SQL授权/版本快照；不以Redis内容或自报hash授权。
比较缓存实际facts、身份、用途策略版本和消息序号，伪造内容即重建。
卡片内容变化（payload指纹）、到期、删除或会话归属/active状态变化均影响下一次使用。
工作索引不缓存邮件对象正文/版本事实；其底层对象即使撤权，索引也不能作为执行凭据，既有卡片工具必须再复核。
此最小实现不宣称缓存消除了数据库查询或已提升性能，目标是先固定有界工作视图和安全一致性。

## 5. 降级、撤权与退出

Redis读/写超时、故障或内容无效时不输出原始异常、URL、正文；记录稳定错误码，使用当前PostgreSQL事实。
所有Redis操作有1秒边界；缓存故障不阻塞正常对话。
SQL身份或会话失效不属于缓存降级：拒绝返回工作索引并尝试清理该用户缓存，不能用缓存绕过数据库。

原消息最多20条，单条4000字符，总计12000字符；工作索引另有完整JSON字节上限。
模型总Prompt仍受原Harness完整输入额度控制，不将字符数称为Token预算。
旧配置关闭时保持原消息装配路径。

当前产品没有会话删除/会话归属撤销HTTP写接口，本阶段不额外引入这些业务。
数据库侧删除/归属变更后，下次读取会即时拒绝并清缓存；即使Redis暂不可达也不能交付旧缓存。
未来若新增这些写接口，应提交数据库事务后调用同一失效协议，不应等待TTL才撤销访问。

`DELETE /session-memory`仅从Bearer取得当前用户，不允许请求指定其他tenant/user，成功204且no-store；
Redis故障503 `SESSION_CACHE_UNAVAILABLE`，不伪装成功。
前端退出前调用该接口，2秒请求边界；失败记录无敏感内容的提示并继续OIDC退出。
这不是注销access token的接口。强制关窗、直接访问OIDC退出或网络断开无法保证主动清理，缓存按TTL过期；
其后任何可见性仍需通过当前认证和SQL核验，缓存不提供离线授权。

## 6. 启用与验证

API先更新，再更新Agent Worker与前端，无新增迁移，不需要重启Index Worker。
在`aegis_agent_worker/.env`设置后重启Agent Worker：

```dotenv
SESSION_MEMORY_ENABLED=true
```

默认false便于灰度。API/Worker应使用同一Redis部署/DB；不要修改队列名来“接入缓存”。
关闭此开关即回到原PostgreSQL历史路径；已有缓存继续TTL过期或主动清理，不影响长期偏好。

手工验证：

1. 同会话先查询邮件，再输入“那封邮件”：近期消息及工作索引可辅助路由，仍需卡片工具核验。
2. 重复读取产生`session_memory_hit`；卡片版本变化或到期产生重建，不能执行旧对象。
3. 换会话/换账号不得读取另一会话工作缓存。
4. 退出前请求`DELETE /session-memory`返回204；缓存键被清理，持久消息/Artifact仍可恢复。
5. 在隔离测试环境模拟Redis故障，普通对话走SQL并记录`session_memory_degraded`。

离线回归默认不连数据库/Redis。可显式启用本地集成测试：

```powershell
# Worker目录；Redis测试仅允许loopback，随机键测试结束清理。
# PG测试只创建随机schema，全部事务回滚，不操作现有表。
$env:AEGIS_SESSION_REDIS_TESTS='1'
$env:AEGIS_SESSION_PG_TESTS='1'
D:\anaconda\envs\Aegis\python.exe -m pytest tests/memory/test_session_memory_live.py -q -p no:cacheprovider
```

实际模型对“同会话追问”的质量及浏览器真实OIDC退出仍需手工联调，不把替身/协议测试当成模型质量或登录端到端验收。
用户在长期记忆页面手动维护摘要与实体的持久化闭环、逐次模型调用记忆审计继续留在7.3/7.4；本阶段不规划对话自动提取候选。

2026-10-17验证结果：Worker完整回归544项通过（含真实本地Redis Lua与回滚PG快照测试）；
API离线回归113项通过、25项其他真实PG测试未启用；前端10项测试通过、生产构建通过。
Redis测试随机键已清除，PG临时schema已回滚，无外部模型/Embedding/Qdrant调用，实际.env未改动。
