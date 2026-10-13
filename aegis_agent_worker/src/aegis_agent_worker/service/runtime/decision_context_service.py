"""主决策需要的业务引用读取、归属核验与审计；不调用模型或选择流程。"""

from aegis_agent_worker.config.database import TenantContext, tenant_transaction
from aegis_agent_worker.repository.decision_repository import DecisionRepository


class DecisionContextService:
    catalog_version = "main-decision-v1"

    def __init__(self, database, runs, events=None, repository=None):
        self._database = database
        self._runs = runs
        self._events = events
        self._repository = repository or DecisionRepository()

    async def _read(self, run, method, *args):
        """每次读取使用独立租户事务，模型推理期间不持有数据库连接。"""
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                return await method(session, run, *args)

    async def load_references(self, run):
        """旧引用读取兼容；生产主决策已改用卡片只读工具，不再调用此方法。"""
        return await self._read(run, self._repository.list_references)

    async def validate_reference(self, run, reference):
        """再次核验目标的真实用户归属与当前版本。"""
        return await self._read(run, self._repository.resolve_reference, reference)

    async def record_decision(
        self, run, *, decision, outcome, duration_ms, capabilities, model_provider, model_name,
        candidate_capabilities=None, filter_reason_codes=None, route_policy_version=None,
        repair_count=0, validation_issue_codes=None,
        context_read_count=0, referenced_cards=None,
        execution_policy_version=None,
    ):
        """持久化脱敏审计事件，事务提交后发布 SSE 通知。"""
        payload = {
            "catalog_version": self.catalog_version, "outcome": outcome,
            "model_provider": model_provider, "model_name": model_name,
            "duration_ms": duration_ms,
            "capabilities": [f"{item.domain}.{item.intent}" for item in capabilities],
            "candidate_capabilities": list(candidate_capabilities or []),
            "filter_reason_codes": list(filter_reason_codes or []),
            "route_policy_version": route_policy_version,
            "repair_count": repair_count,
            "validation_issue_codes": list(validation_issue_codes or []),
            "context_read_count": context_read_count,
            "referenced_cards": list(referenced_cards or []),
            "execution_policy_version": execution_policy_version,
            "decision": decision.decision if decision else None,
            "domain": decision.domain if decision else None,
            "intent": decision.intent if decision else None,
            "target": decision.target.model_dump(mode="json") if decision and decision.target else None,
        }
        # 路由摘要持久化到事件表，Trace 关联到模型 HTTP 调用。
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                event = await self._runs.append_event(
                    session, run_id=run.id, tenant_id=run.tenant_id,
                    event_type="main_decision_completed", payload=payload,
                )
                if self._events is not None:
                    self._events.publish_after_commit(event)
