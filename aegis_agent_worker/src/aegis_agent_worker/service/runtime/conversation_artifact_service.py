"""短期对话 Artifact 的应用服务。"""

from aegis_agent_worker.agent.contracts.common import ConversationArtifact
from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.event.run_event import RunEventPublisher
from aegis_agent_worker.repository.conversation_artifact_repository import ConversationArtifactRepository
from aegis_agent_worker.repository.run_repository import AgentRunRepository, ClaimedRun


class ConversationArtifactService:
    """在租户事务内保存卡片引用，并提交后通知 SSE 客户端刷新。"""

    def __init__(
        self,
        database: Database,
        runs: AgentRunRepository,
        events: RunEventPublisher | None = None,
        artifacts: ConversationArtifactRepository | None = None,
    ) -> None:
        self._database = database
        self._runs = runs
        self._events = events
        self._artifacts = artifacts or ConversationArtifactRepository()

    async def persist(self, run: ClaimedRun, artifacts: list[ConversationArtifact]) -> list[str]:
        """保存非空 Artifact，并在事务提交后发布轻量刷新事件。"""
        if not artifacts or run.conversation_id is None:
            return []
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                identifiers = await self._artifacts.upsert_for_run(
                    session, run=run, artifacts=artifacts
                )
                if self._events is not None:
                    event = await self._runs.append_event(
                        session,
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        event_type="artifacts_ready",
                        payload={
                            "conversation_id": str(run.conversation_id),
                            "artifact_ids": [str(identifier) for identifier in identifiers],
                            "artifact_types": [artifact.artifact_type for artifact in artifacts],
                        },
                    )
                    self._events.publish_after_commit(event)
        return [str(identifier) for identifier in identifiers]
