"""邮件规划审计服务，不调用 LLM，也不决定下一步动作。"""

from aegis_agent_worker.config.database import TenantContext, tenant_transaction


class MailPlanningService:
    def __init__(self, database, runs, events=None):
        self._database, self._runs, self._events = database, runs, events

    async def record_plan(self, run, *, decision, outcome, planning_source, duration_ms,
                          model_provider, model_name):
        payload = {
            "version": 1, "action": decision.action if decision else None,
            "target": decision.target.model_dump(mode="json") if decision and decision.target else None,
            "outcome": outcome, "planning_source": planning_source, "duration_ms": duration_ms,
            "model_provider": model_provider, "model_name": model_name,
        }
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                event = await self._runs.append_event(session, run_id=run.id, tenant_id=run.tenant_id,
                                                      event_type="mail_plan_completed", payload=payload)
                if self._events:
                    self._events.publish_after_commit(event)
