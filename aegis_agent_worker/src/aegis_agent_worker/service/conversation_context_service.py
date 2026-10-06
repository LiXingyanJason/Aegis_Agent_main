"""为 Agent 加载受租户限制的会话上下文。"""

from typing import Any

from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.repository.run_repository import AgentRunRepository, ClaimedRun


class ConversationContextService:
    """将数据库读取与 Agent 的上下文装配边界分开。"""

    def __init__(self, database: Database, runs: AgentRunRepository) -> None:
        self._database = database
        self._runs = runs

    async def load_history(
        self, run: ClaimedRun, context: TenantContext
    ) -> list[dict[str, Any]]:
        """读取该任务关联会话的最近可见消息。"""
        async with self._database.session() as session:
            # 读取该会话的历史消息
            # eg:[{"role": "user", "content": "查看我明天的日程"},
            #     {"role": "assistant", "content": "此前的回复"},
            #     {"role": "user", "content": "那下午有空吗？"}]
            async with tenant_transaction(session, context):
                return await self._runs.load_conversation_history(
                    session,
                    conversation_id=run.conversation_id,
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                )
