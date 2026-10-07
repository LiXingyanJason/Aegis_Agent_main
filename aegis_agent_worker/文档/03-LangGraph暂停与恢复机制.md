# LangGraph 暂停与恢复机制

## 1. 目的

Aegis PA 中，读取日程等只读工具可以直接执行；创建日历事件、发送邮件等外部副作用必须在用户逐项确认后才能执行。

为实现“执行到确认点暂停，用户决定后继续原任务”，Worker 使用：

```text
LangGraph interrupt
+ PostgreSQL Checkpointer
+ Redis List 任务队列
+ Redis Pub/Sub 进度通知
+ Aegis 业务审批表
```

## 2. 核心标识

一个任务运行使用同一个 `run_id` 贯穿各层：

```text
agent_runs.id
= Redis 队列消息中的 run_id
= LangGraph thread_id
= checkpoints.thread_id
= checkpoint_blobs.thread_id
= checkpoint_writes.thread_id
= Redis Pub/Sub 频道 aegis:run:{run_id}:events 的一部分
```

业务表中 `run_id` 是 PostgreSQL `uuid`；LangGraph Checkpointer 的 `thread_id` 是文本，因此调用图时使用 `str(run.id)`。

## 3. 哪些操作需要暂停

是否暂停是 Aegis 的业务规则，不是 LangGraph 自动判断。

| 操作类型 | 示例 | 是否暂停 |
|---|---|---|
| `read` | `calendar.list_events`、`calendar.find_free_time` | 否 |
| 站内草稿 | 创建 `calendar_event_drafts` | 否 |
| `write` | `calendar.create_event` | 是，等待逐项批准 |
| `sensitive` | 后续 `mail.send` | 是，等待逐项批准 |

当前会议安排由意图路由进入 `calendar_confirmation` 分支。该分支完成空闲时间查询、创建会议草稿和 `approval_item` 后，图边进入 `wait_for_calendar_approval` 节点。

```text
START
→ load_context
→ decide_workflow
→ calendar_confirmation
→ wait_for_calendar_approval
→ 批准/拒绝后继续
```

## 4. 暂停点

暂停节点中的关键代码是：

```python
decision = interrupt(
    {
        "run_id": state["run_id"],
        "draft_id": state.get("draft_id"),
        "approval_item_id": state.get("approval_item_id"),
        "status": "waiting_confirmation",
    }
)
```

`interrupt(...)` 表示：

```text
此处不可继续执行
→ 保存当前图状态与下一执行位置
→ 等待后续 Command(resume=...) 输入
```

项目不直接执行 `INSERT INTO checkpoints`。根图使用 `compile(checkpointer=checkpointer)` 编译后，LangGraph 在执行到 `interrupt(...)` 时自动保存 Checkpoint，并让 `ainvoke()` 返回包含 `__interrupt__` 的状态。

Worker 检测结果：

```python
if state.get("__interrupt__"):
    return AgentExecutionResult(status="waiting_confirmation")
```

此时 Worker 本轮处理结束，回到 Redis 队列继续等待其他任务。

## 5. 暂停时保存的内容

### 5.1 Aegis 业务事实

在调用 `interrupt(...)` 前，会议草稿服务已在 Aegis PostgreSQL 业务表中保存：

```text
agent_runs.status = waiting_confirmation
calendar_event_drafts.status = pending_approval
approval_items.status = pending
run_events 中新增 approval_required
```

这些表是业务真实性和审计的权威来源。

### 5.2 LangGraph 执行快照

LangGraph Checkpointer 自动维护：

| 表 | 关键字段 | 用途 |
|---|---|---|
| `checkpoints` | `thread_id`、`checkpoint_ns`、`checkpoint_id`、`checkpoint`、`metadata` | 图状态、节点位置及快照元数据 |
| `checkpoint_blobs` | `thread_id`、`channel`、`version`、`blob` | 较大状态通道的数据 |
| `checkpoint_writes` | `thread_id`、`checkpoint_id`、`task_id`、`channel`、`blob` | 节点待合并或中间状态写入 |
| `checkpoint_migrations` | `v` | Checkpointer 表结构迁移版本 |

Checkpoint 是流程恢复数据，不应替代 `approval_items`、`calendar_event_drafts`、`external_action_executions` 等业务表。

## 6. Redis 的两种用途

### 6.1 Redis Pub/Sub：实时 SSE 通知

Worker 将已提交的 `run_events` 发布到：

```text
aegis:run:{run_id}:events
```

例如：

```text
approval_required
progress_updated
tool_preview
approval_executed
run_completed
```

API 的 SSE 请求订阅该频道，并把事件转给浏览器。Pub/Sub 本身不持久化消息；断线补发依赖 PostgreSQL 的 `run_events`，由 `Last-Event-ID` 查询 `event_no > Last-Event-ID` 的全部事件。

### 6.2 Redis List：Worker 任务队列

任务队列名称为：

```text
aegis:agent-runs
```

API 使用 `RPUSH` 投递，Worker 使用 `BLPOP` 消费。

普通新任务消息：

```json
{"run_id":"..."}
```

审批后的恢复消息：

```json
{"run_id":"...","resume_decision":"approved"}
```

暂停中的任务不会继续占用 Redis List。Worker 已经消费了原始任务；等待状态由业务表和 Checkpoint 保存。

## 7. 批准或拒绝后的恢复流程

```text
用户在确认页提交 approved / rejected
↓
POST /approvals/{approval_item_id}/decision
↓
API 在同一事务内：
  写 approval_decisions
  更新 approval_items
↓
数据库提交成功
↓
after_commit 回调向 Redis List RPUSH：
  {run_id, resume_decision}
↓
Worker BLPOP 获取恢复消息
↓
Worker 二次校验：
  agent_runs.status = waiting_confirmation
  数据库中存在同 run_id、同 decision 的审批记录
↓
agent_runs.status → running
↓
GraphRunner.resume(run_id, decision)
↓
LangGraph 按 thread_id = run_id 读取最新 Checkpoint
↓
将 Command(resume=decision) 作为 interrupt(...) 的返回值
↓
继续图的批准或拒绝分支
```

恢复调用：

```python
await self._graph.ainvoke(
    Command(resume=decision),
    {"configurable": {"thread_id": str(run_id)}},
)
```

恢复后：

```text
approved
→ 再次核验批准项和草稿
→ 写 external_action_executions
→ 调用 calendar.create_event
→ 成功时更新草稿、确认项为 executed

rejected
→ 不调用日历工具
→ 生成“不会创建日历”的最终回复
```

## 8. 启动与初始化

Worker 启动时创建 `AsyncPostgresSaver` 并执行：

```python
await checkpointer.setup()
```

当前开发实现每次 Worker 进程启动都会执行一次。该操作是幂等的：表不存在时创建，已存在时进行版本检查，不会为每个任务建表。生产环境可将初始化迁移到部署或数据库迁移阶段统一处理。

## 9. 安全边界

1. 浏览器不能直接调用 `calendar.create_event`，也不持有 MCP 凭据。
2. `Command(resume=...)` 的来源不是唯一信任依据；Worker 仍会在数据库中二次验证任务和审批决定。
3. 真正写入前再次锁定并验证 `approval_items.status = approved_executing`、`approval_decisions.decision = approved`。
4. `external_action_executions.idempotency_key` 防止相同批准操作重复创建日历事件。
5. `run_events` 是 SSE 断线恢复依据；Redis Pub/Sub 只承担低延迟实时通知。
