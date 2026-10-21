"""通用执行编排：上下文、策略、预算、超时和审计；不含SQL或模型通信。"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.harness.context import bind_execution, get_execution, reset_execution
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.harness.failures import execution_error_code
from aegis_agent_worker.harness.timeout import execution_timeout
from aegis_agent_worker.harness.policies.execution import TaskExecutionPolicy
from aegis_agent_worker.harness.policy_provider import PolicyProvider, PolicyBundle, DEFAULT_POLICIES
from aegis_agent_worker.repository.execution_repository import ExecutionRepository
from aegis_agent_worker.repository.run_repository import ClaimedRun


class AttemptAudit(dict):
    """调用元数据仍为字典；attempt ID不是finish_attempt的可变更新字段。"""
    def __init__(self, attempt_id):
        super().__init__()
        self.attempt_id = attempt_id


class HarnessExecutor:
    """调用 HarnessExecutor.attempt() 接入执行治理"""
    def __init__(
        self, database: Database, policy: TaskExecutionPolicy | None = None,
        repository: ExecutionRepository | None = None,
        policy_provider: PolicyProvider | None = None,
    ) -> None:
        self._database = database
        if policy is not None and policy_provider is not None:
            raise ValueError("不能同时注入单一策略和策略Provider")
        self._policies = policy_provider or (PolicyProvider((PolicyBundle(execution=policy),), policy.version)
                                             if policy is not None else DEFAULT_POLICIES)
        self._repository = repository or ExecutionRepository()

    @property
    def policy(self):
        return self._policies.current().execution

    @asynccontextmanager
    async def _session(self, run: ClaimedRun) -> AsyncIterator[AsyncSession]:
        # 给本次run提供绑定租户/用户上下文的会话，保证SQL受数据隔离约束。
        async with self._database.session() as session:
            context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
            async with tenant_transaction(session, context):
                yield session

    @asynccontextmanager
    async def scope(self, run: ClaimedRun) -> AsyncIterator[None]:
        """划定一次消费的边界，持久化真实耗时，供恢复/限时判断使用。"""
        async with self._session(run) as session:
            version = await self._repository.ensure_budget(session, run, self._policies.default_version)
            profile = self._policies.resolve(version)
        token = bind_execution(run, profile)
        try:
            yield
        finally:
            runtime = get_execution()
            reset_execution(token)
            async with self._session(run) as session:
                await self._repository.add_active_time(
                    session, run, round(runtime.elapsed_seconds * 1000),
                )

    @asynccontextmanager
    async def attempt(
        self, kind: str, label: str, risk_flags: list[str] | None = None,
    ) -> AsyncIterator[dict]:
        """白名单 + 次数 + 时长 + 超时四重约束，并逐次审计入库。"""
        runtime = get_execution()  # 校验上下文
        run = runtime.run
        if kind == "tool" and label not in self.policy.tools(run.run_type):
            raise HarnessError("TOOL_SCOPE_BLOCKED", "当前任务不允许调用该工具")
        maximum = self.policy.call_limit(kind)
        async with self._session(run) as session:
            # 原子预算扣减和创建审计记录位于同一事务，失败时一起回滚。
            active_ms = await self._repository.reserve_call(
                session, run, kind=kind, maximum=maximum, policy_version=self.policy.version,
                elapsed_ms=round(runtime.elapsed_seconds * 1000),
                time_limit_ms=self.policy.active_seconds * 1000,
            )
            if active_ms is None:
                # 区分取消/状态变化与预算不足；原子占用SQL本身也检查running，防止预检后取消。
                await self._check_snapshot(session, runtime, require_model=False, require_tool=False)
                raise HarnessError("EXECUTION_BUDGET_EXCEEDED", "任务执行次数或时间预算已耗尽")
            remaining = self.policy.active_seconds - active_ms / 1000 - runtime.elapsed_seconds
            attempt_id = await self._repository.start_attempt(
                session, run, kind=kind, label=label, risk_flags=risk_flags,
            )
        code, status = None, "succeeded"
        audit = AttemptAudit(attempt_id)
        try:  # 执行 + 超时控制；等待模型/工具时不持有数据库事务。
            async with execution_timeout(min(self.policy.call_timeout(kind), max(0.01, remaining))):
                yield audit
        except BaseException as error:
            status, code = "failed", execution_error_code(error)
            if isinstance(error, TimeoutError):
                code = "EXECUTION_TIMEOUT"
                raise HarnessError("EXECUTION_TIMEOUT", "任务执行超时") from error
            raise
        finally:  # 收尾落库
            async with self._session(run) as session:
                await self._repository.finish_attempt(
                    session, attempt_id, status=status, error_code=code,
                    **audit,
                )

    async def _check_snapshot(self, session, runtime, *, require_model, require_tool):
        row = await self._repository.snapshot(session, runtime.run)
        if row is None:
            raise HarnessError("EXECUTION_STATE_UNAVAILABLE", "无法核验任务执行状态")
        if row["run_status"] == "cancelled":
            raise HarnessError("RUN_CANCELLED", "任务已取消，停止后续执行")
        if row["run_status"] != "running":
            raise HarnessError("EXECUTION_STATE_CHANGED", "任务已不处于可执行状态")
        if row["policy_version"] != self.policy.version:
            raise HarnessError("POLICY_VERSION_CONFLICT", "执行策略版本与任务预算不一致")
        if (row["active_ms"]+round(runtime.elapsed_seconds*1000) >= self.policy.active_seconds*1000
                or require_model and row["model_calls"] >= self.policy.max_models
                or require_tool and row["tool_calls"] >= self.policy.max_tools):
            raise HarnessError("EXECUTION_BUDGET_EXCEEDED", "任务执行次数或时间预算已耗尽")

    async def check_remaining(self, *, require_model=True, require_tool=True):
        """委派前只读预检，不扣预算；真实调用仍必须原子reserve_call。"""
        runtime = get_execution()
        async with self._session(runtime.run) as session:
            await self._check_snapshot(session, runtime, require_model=require_model, require_tool=require_tool)
