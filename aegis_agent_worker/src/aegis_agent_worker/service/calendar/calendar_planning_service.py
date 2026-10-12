"""日历领域计划审计，不调用模型或选择工具。"""

from aegis_agent_worker.config.database import TenantContext, tenant_transaction


class CalendarPlanningService:
    def __init__(self, database, runs, events=None):
        self._database, self._runs, self._events = database, runs, events

    async def record_plan(self, run, *, decision, outcome, duration_ms, model_provider, model_name, validation_issues=None):
        """持久化最小决策摘要，不包含主题、参会人邮箱或原始 Prompt。"""
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        payload = {
            "version": 1, "outcome": outcome, "duration_ms": duration_ms,
            "model_provider": model_provider, "model_name": model_name,
            "action": decision.action if decision else None,
            "time_mode": decision.time_mode if decision else None,
            "validation_issues": validation_issues or [],
        }
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                event = await self._runs.append_event(
                    session, run_id=run.id, tenant_id=run.tenant_id,
                    event_type="calendar_plan_completed", payload=payload,
                )
                if self._events:
                    self._events.publish_after_commit(event)
