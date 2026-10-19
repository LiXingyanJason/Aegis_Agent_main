# Agent Harness

Harness负责“允许执行什么、最多执行多少、如何限制并记录”，不负责LLM决策。

| 位置 | 职责 |
| --- | --- |
| `policies/execution.py` | 工具白名单、模型／工具次数、执行时限及字符上限 |
| `policies/workflow.py` | 主决策读取／轮次、邮件收集轮次等局部限制 |
| `policies/output.py` | 有界修复、只读重试、输出大小与提示词裁剪 |
| `policy_provider.py` | 完整策略包版本及旧版本恢复；policy.py仅是旧导入兼容 |
| `context.py` | 当前run及本次消费开始时间，使用ContextVar随协程传播 |
| `executor.py` | 事务编排、原子预算检查、超时及审计收尾，不编写SQL |
| `exceptions.py` | 稳定错误码与安全错误文本 |
| `../repository/execution_repository.py` | 预算初始化／扣减、耗时累计、执行记录SQL |
| `../llm/guarded_client.py` | 模型输入输出边界、提示注入风险标记、调用接入Harness |

## 调用关系

```text
Worker组装 → HarnessExecutor + GuardedLLMClient + ToolGateway
Orchestrator → executor.scope(run)
  ├─ MailAgent → GuardedLLMClient → executor.attempt("model", ...)
  └─ 工具Service → ToolGateway → executor.attempt("tool", ...)
                                 ├─ ExecutionRepository：扣减预算／创建审计
                                 ├─ yield：交给模型／工具调用，不持有数据库事务
                                 └─ ExecutionRepository：记录调用结果
```

`scope()`按run初始化预算，恢复时不重置；结束时累计活跃时间。用户等待不在scope内。
预算扣减与审计开始处于同一短事务，调用前检查可信run_type白名单。事务绑定tenant_id、user_id。
默认10次模型调用、6次工具尝试、120秒累计活跃时间，单次模型60秒／工具20秒。

## 扩展边界

- 调整工具范围或预算：注册新的PolicyBundle版本并保留旧版本，模型不能改变策略。
- 新增预算／审计持久化：扩展`ExecutionRepository`，Executor允许注入仓库以独立测试。
- 新模型供应商：修改原始LLMClient，保持`GuardedLLMClient`外层包装。
- 参数、连接归属、外部写入审批：仍由ToolGateway和领域业务服务检查。
- JSON格式修复：仍在MailAgent，修复通过包装器计数；Harness不修复业务输出。
- 补充问答：仍属于`service/runtime/input_request_service.py`，不是Harness的职责。

此重构不改变API、Redis消息或数据库表，不新增迁移。旧`service/runtime/execution_harness.py`
已拆分删除，内部导入已全部迁移。修改后重启Worker即可。

局部常量已集中管理，运行期从scope绑定的run策略包读取，不由模型或请求覆盖。
首次任务固定run_execution_budgets.policy_version，恢复沿用原版及已有计数，无新增迁移。
详见[集中策略说明](../../../文档/阶段1/07-执行与工作流限制集中策略.md)。
