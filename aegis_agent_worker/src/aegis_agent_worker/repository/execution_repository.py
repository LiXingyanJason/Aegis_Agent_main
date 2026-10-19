"""执行预算、活跃耗时和逐次审计的SQL；事务由调用方管理。"""
import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aegis_agent_worker.repository.run_repository import ClaimedRun


class ExecutionRepository:
    async def ensure_budget(self, session: AsyncSession, run: ClaimedRun, version: str) -> str:
        """首次固定策略包版本；恢复返回原版本，不重置次数／耗时或改写版本。"""
        result = await session.execute(text("""
            INSERT INTO run_execution_budgets(run_id,tenant_id,user_id,policy_version)
            VALUES(:id,:tenant,:user,:policy) ON CONFLICT(run_id) DO UPDATE SET run_id=EXCLUDED.run_id
            WHERE run_execution_budgets.tenant_id=EXCLUDED.tenant_id
              AND run_execution_budgets.user_id=EXCLUDED.user_id
            RETURNING policy_version
        """), dict(id=run.id, tenant=run.tenant_id, user=run.user_id, policy=version))
        return result.mappings().one()["policy_version"]

    async def add_active_time(self, session: AsyncSession, run: ClaimedRun, elapsed_ms: int) -> None:
        await session.execute(text("""
            UPDATE run_execution_budgets SET active_ms=active_ms+:elapsed
            WHERE run_id=:id AND tenant_id=:tenant
        """), dict(elapsed=elapsed_ms, id=run.id, tenant=run.tenant_id))

    async def reserve_call(
        self, session: AsyncSession, run: ClaimedRun, *, kind: str,
        maximum: int, policy_version: str, elapsed_ms: int, time_limit_ms: int,
    ) -> int | None:
        """原子占用预算，返回已累计活跃毫秒；条件不满足时返回None。"""
        # 动态列名只来自代码白名单，不能由模型或请求提供SQL片段。
        fields = {"model": "model_calls", "tool": "tool_calls"}
        if kind not in fields:
            raise ValueError("未知执行调用类型")
        field = fields[kind]
        result = await session.execute(text(f"""
            UPDATE run_execution_budgets SET {field}={field}+1
            WHERE run_id=:id AND tenant_id=:tenant AND {field}<:maximum
              AND policy_version=:policy AND active_ms+:elapsed<:time_limit
            RETURNING active_ms
        """), dict(id=run.id, tenant=run.tenant_id, maximum=maximum, policy=policy_version,
                   elapsed=elapsed_ms, time_limit=time_limit_ms))
        row = result.mappings().first()
        return row["active_ms"] if row is not None else None

    async def start_attempt(
        self, session: AsyncSession, run: ClaimedRun, *, kind: str,
        label: str, risk_flags: list[str] | None = None,
    ) -> UUID:
        result = await session.execute(text("""
            INSERT INTO run_execution_attempts(run_id,tenant_id,user_id,kind,label,risk_flags)
            VALUES(:id,:tenant,:user,:kind,:label,CAST(:risk AS jsonb)) RETURNING id
        """), dict(id=run.id, tenant=run.tenant_id, user=run.user_id,
                   kind=kind, label=label, risk=json.dumps(risk_flags or [])))
        return result.mappings().one()["id"]

    async def finish_attempt(
        self, session: AsyncSession, attempt_id: UUID, *, status: str, error_code: str | None,
        purpose: str | None = None, model_provider: str | None = None, model_name: str | None = None,
        prompt_tokens: int | None = None, completion_tokens: int | None = None,
        total_tokens: int | None = None,
        memory_count: int | None = None, memory_policy_version: str | None = None,
        memory_selection_reason: str | None = None,
        memory_selection_details: dict | None = None,
    ) -> None:
        await session.execute(text("""
            UPDATE run_execution_attempts
            SET status=:status,error_code=:code,finished_at=now(),purpose=:purpose,
                model_provider=:provider,model_name=:model,prompt_tokens=:prompt,
                completion_tokens=:completion,total_tokens=:total,
                memory_count=:memory_count,memory_policy_version=:memory_policy,
                memory_selection_reason=:memory_reason,memory_selection_details=CAST(:memory_details AS jsonb) WHERE id=:id
        """), dict(status=status, code=error_code, id=attempt_id, purpose=purpose,
                   provider=model_provider, model=model_name, prompt=prompt_tokens,
                   completion=completion_tokens, total=total_tokens,memory_count=memory_count,
                   memory_policy=memory_policy_version,memory_reason=memory_selection_reason,
                   memory_details=json.dumps(memory_selection_details) if memory_selection_details is not None else None))
