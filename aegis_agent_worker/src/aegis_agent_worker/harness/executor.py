"""通用执行编排：上下文、策略、预算、超时和审计；不含SQL或模型通信。"""
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.harness.context import bind_execution, get_execution, reset_execution
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.harness.policy import TaskExecutionPolicy
from aegis_agent_worker.repository.execution_repository import ExecutionRepository
from aegis_agent_worker.repository.run_repository import ClaimedRun


class HarnessExecutor:
    """调用 HarnessExecutor.attempt() 接入执行治理"""
    def __init__(
        self, database: Database, policy: TaskExecutionPolicy | None = None,
        repository: ExecutionRepository | None = None,
    ) -> None:
        self._database = database
        self.policy = policy or TaskExecutionPolicy()
        self._repository = repository or ExecutionRepository()

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
            await self._repository.ensure_budget(session, run, self.policy.version)
        token = bind_execution(run)
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
    ) -> AsyncIterator[None]:
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
                raise HarnessError("EXECUTION_BUDGET_EXCEEDED", "任务执行次数或时间预算已耗尽")
            remaining = self.policy.active_seconds - active_ms / 1000 - runtime.elapsed_seconds
            attempt_id = await self._repository.start_attempt(
                session, run, kind=kind, label=label, risk_flags=risk_flags,
            )
        code, status = None, "succeeded"
        try:  # 执行 + 超时控制；等待模型/工具时不持有数据库事务。
            async with asyncio.timeout(min(self.policy.call_timeout(kind), max(0.01, remaining))):
                yield
        except BaseException as error:
            status, code = "failed", getattr(error, "code", "EXECUTION_FAILED")
            if isinstance(error, TimeoutError):
                code = "EXECUTION_TIMEOUT"
                raise HarnessError("EXECUTION_TIMEOUT", "任务执行超时") from error
            raise
        finally:  # 收尾落库
            async with self._session(run) as session:
                await self._repository.finish_attempt(
                    session, attempt_id, status=status, error_code=code,
                )
