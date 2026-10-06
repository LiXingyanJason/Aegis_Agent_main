"""Agent 任务生命周期、步骤与 SSE 事件的事务性编排。"""

from uuid import UUID

from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.event.run_event import RunEventPublisher
from aegis_agent_worker.repository.run_repository import AgentRunRepository, ClaimedRun


class RunLifecycleService:
    """集中管理任务状态迁移，确保业务写入与事件记录处于同一事务。"""

    def __init__(
        self,
        database: Database,
        runs: AgentRunRepository,
        events: RunEventPublisher | None = None,
    ) -> None:
        self._database = database
        self._runs = runs
        self._events = events

    async def claim_queued_run(
        self, run_id: UUID, *, model_provider: str, model_name: str
    ) -> ClaimedRun | None:
        """原子领取 queued 任务，并在成功提交后发布任务开始事件。"""
        started_event = None
        # 抢到一个任务 将任务标为运行中 写入“任务已启动”事件 这三者保证保持一致(要么全部成功并提交，要么全部回滚)
        async with self._database.session() as session:
            async with session.begin():
                run = await self._runs.claim_queued_run(
                    session,
                    run_id,
                    model_provider=model_provider,
                    model_name=model_name,
                ) # 只有一个 Worker 能把该任务从 queued 改为 running 其他 Worker 会得到 None，不会重复调用 LLM
                # 通过数据库sql查询实现(SET status = 'running')
                if run is not None and self._events is not None:
                    # 成功抢到任务，并且系统启用了事件发布器时，记录事件
                    started_event = await self._runs.append_event( # 持久化该事件
                        session,
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        event_type="run_started",
                        payload={
                            "status": "running",
                            "current_stage": "正在调用模型",
                            "model_provider": model_provider,
                            "model_name": model_name,
                        },
                    )
        if started_event is not None and self._events is not None:
            await self._events.publish_best_effort(started_event) # 发布 Redis 事件 started_event
        return run

    async def start_llm_generation(self, run: ClaimedRun, context: TenantContext) -> UUID:
        """写入 LLM 执行步骤，并在事务提交后发送前端进度。"""
        # 在数据库中登记“模型生成回复”，并在事务提交后通知前端
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                step_id = await self._runs.create_running_llm_step(
                    session, run_id=run.id, tenant_id=run.tenant_id
                )
                if self._events is not None:
                    event = await self._runs.append_event(
                        session,
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        event_type="progress_updated",
                        payload={
                            "step_id": str(step_id),
                            "label": "正在生成回复",
                            "status": "running",
                        },
                    )
                    self._events.publish_after_commit(event)
        return step_id

    async def resume_waiting_confirmation_run(
        self, run_id: UUID, *, decision: str
    ) -> ClaimedRun | None:
        """仅恢复数据库中已有相同确认决定的等待任务。"""
        if decision not in {"approved", "rejected"}:
            return None
        async with self._database.session() as session:
            async with session.begin():
                return await self._runs.claim_waiting_confirmation_run(
                    session, run_id, decision=decision
                )

    async def complete_run(
        self,
        run: ClaimedRun,
        context: TenantContext,
        *,
        assistant_content: str,
        step_id: UUID | None,
    ) -> None:
        """保存助手回复、完成任务，并写入最终 SSE 事件。"""
        # 数据库保存最终结果、完成任务、发布完成事件
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                await self._runs.complete_run_with_assistant_message(
                    session,
                    run_id=run.id,
                    conversation_id=run.conversation_id,
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                    assistant_content=assistant_content,
                    step_id=step_id,
                )
                if self._events is not None:
                    message_event = await self._runs.append_event(
                        session,
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        event_type="assistant_message_completed",
                        payload={"content": assistant_content, "is_final": True},
                    )
                    completed_event = await self._runs.append_event(
                        session,
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        event_type="run_completed",
                        payload={"status": "completed", "result_summary": assistant_content[:1000]},
                    )
                    self._events.publish_after_commit(message_event)
                    self._events.publish_after_commit(completed_event)

    async def fail_run(
        self,
        run: ClaimedRun,
        *,
        step_id: UUID | None,
        error_code: str,
        error_message: str,
    ) -> None:
        """在可确定的租户上下文中记录执行失败，避免 Worker 直接向外抛出业务异常。"""
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                await self._runs.fail_run(
                    session,
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    step_id=step_id,
                    error_code=error_code,
                    error_message=error_message,
                )
                if self._events is not None:
                    event = await self._runs.append_event(
                        session,
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        event_type="run_failed",
                        payload={
                            "status": "failed",
                            "error_code": error_code,
                            "error_message": error_message,
                        },
                    )
                    self._events.publish_after_commit(event)
