# Aegis PA 安全、运维与验收说明

## 1. 安全控制矩阵

| 威胁/风险 | 控制措施 | 强制点 |
|---|---|---|
| Prompt Injection | NeMo 输入规则、外部内容标记为不可信、工具参数由 Schema 而非自然语言生成 | 输入与检索结果注入前 |
| 越权工具调用 | OAuth scopes、RBAC、工具 allowlist、最小权限 | 每次工具调用前 |
| 非预期副作用 | 风险分级、参数预览、一次性确认令牌、幂等键 | 邮件/日历写入/代码执行前 |
| PII 泄露 | PII 检测与掩码、日志脱敏、字段级加密、Secret Manager | 输出、trace、审计写入前 |
| 恶意代码 | 独立容器沙箱、无网络、只读根文件系统、CPU/内存/时长限制 | 代码执行工具 |
| 供应链与密钥风险 | 镜像扫描与签名、依赖锁定、K8s Secret 外部托管、轮换 | CI/CD 与运行时 |

### 风险等级与默认策略

| 等级 | 示例 | 默认动作 |
|---|---|---|
| `read` | 搜索、读取日程、检索邮件 | 在已授权 scope 内直接执行 |
| `write` | 创建日程、修改草稿 | 预览后确认 |
| `sensitive` | 发送邮件、读取敏感会话 | 明确确认、强审计、必要时二次认证 |
| `code_execute` | 运行用户代码 | 明确确认；仅隔离沙箱 |

## 2. 监控与告警

Prometheus 指标至少包括：`agent_requests_total`、`agent_request_latency_seconds`、`llm_tokens_total`、`llm_estimated_cost_total`、`model_route_total`、`tool_calls_total`、`tool_call_latency_seconds`、`guardrail_blocks_total`、`confirmation_total`、`memory_retrieval_latency_seconds`。

告警建议：API 5xx 比例 > 2%（5 分钟）、P95 时延超 SLO（15 分钟）、工具失败率 > 5%、Guardrail 拦截率突增、模型成本异常、确认令牌校验失败激增。LangSmith 采样 trace 时必须对输入/输出执行脱敏，并保留 `request_id` 与 `trace_id` 关联。

## 3. Docker 与 Kubernetes

- 镜像分为 `api`、`worker`、`migration`；使用非 root 用户、固定基础镜像摘要和只读文件系统。
- API 与 Worker 分离扩缩容；Worker 用队列处理摘要、embedding、重试和清理任务。
- 配置来自 ConfigMap；凭据仅使用 External Secrets/KMS 引用。禁止把 API key 写入镜像或日志。
- 设置 readiness/liveness probes、resource requests/limits、PDB、HPA、NetworkPolicy 和滚动发布策略。
- 数据库 migration 通过单独 Job 在部署前执行；备份、恢复演练、向量索引重建需纳入 Runbook。

## 4. 验收标准

| 类别 | 验收项 |
|---|---|
| 路由 | 基准集上意图分类准确率、模型路由准确率达到团队设定阈值；所有路由记录成本与时延 |
| 工具 | MCP 工具可注册、发现、Schema 校验；失败能返回标准错误；写操作具备幂等性 |
| 记忆 | 同一用户可召回有效偏好；跨用户/租户检索结果为零；删除后不可再检索 |
| 安全 | 注入测试不能提升工具权限；PII 不出现在普通日志；写操作无确认不得执行 |
| 可观测性 | 一次请求可从 API、LangGraph、LLM、工具到审计事件按 trace_id 串联 |
| 部署 | Docker 一键启动开发依赖；K8s 滚动升级不丢失进行中的运行状态 |

## 5. 建议交付里程碑

1. **MVP**：对话、单模型、日历只读/预览、Redis 会话、基础审计。
2. **Beta**：多模型路由、邮件与搜索 MCP、确认网关、长期记忆、LangSmith/Prometheus。
3. **Production**：NeMo 规则治理、代码沙箱、SLO/告警、Kubernetes、压力与红队测试、A/B 实验。
