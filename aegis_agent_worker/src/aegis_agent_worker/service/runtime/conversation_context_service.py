"""为 Agent 加载受租户限制的会话上下文。"""

from typing import Any

from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.repository.run_repository import AgentRunRepository, ClaimedRun
from aegis_agent_worker.harness.policy_provider import DEFAULT_POLICIES


class ConversationContextService:
    """将数据库读取与 Agent 的上下文装配边界分开。"""

    def __init__(self, database: Database, runs: AgentRunRepository, *, session_memory=None,
                 policy_provider=None) -> None:
        self._database = database
        self._runs = runs
        self._session_memory = session_memory
        self._policies = policy_provider or DEFAULT_POLICIES

    async def load_history(
        self, run: ClaimedRun, context: TenantContext
    ) -> list[dict[str, Any]]:
        """
        为 LLM 装配本次对话上下文，返回的是一个消息列表，不会修改数据库里的历史消息。
        例如原历史是 [用户：查邮件, 助手：找到卡片, 用户：给第一封起草回复]
        返回结果可能是 [用户：查邮件, 助手：找到卡片, 工作索引：卡片 ID 等, 用户：给第一封起草回复]
        """
        if (run.tenant_id,run.user_id)!=(context.tenant_id,context.user_id):
            raise ValueError('SESSION_IDENTITY_MISMATCH')
        async with self._database.session() as session:
            # 读取该会话的历史消息
            # eg:[{"role": "user", "content": "查看我明天的日程"},
            #     {"role": "assistant", "content": "此前的回复"},
            #     {"role": "user", "content": "那下午有空吗？"}]
            async with tenant_transaction(session, context):
                history = await self._runs.load_conversation_history(
                    session,
                    conversation_id=run.conversation_id,
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                )
        if self._session_memory is None:
            return history
        facts=await self._session_memory.load(run,context)
        policy=self._policies.current().session_memory
        # 保留最新消息，按条数/字符装配；模型总输入仍由Harness检查。
        selected=[]
        remaining=policy.history_chars
        for item in reversed(history[-policy.history_items:]):
            content=str(item['content'])[:min(policy.message_chars,remaining)]
            if not content: break
            selected.append({'role':item['role'],'content':content})
            remaining-=len(content)
        selected.reverse()
        if facts and any(facts.values()) and selected:
            summary=self._session_memory.format_data(facts)
            # 放在最后一条真实消息之前，不冒充最新用户请求；不写回消息数据库。
            selected.insert(len(selected)-1,{'role':'user','content':summary,'context_kind':'session_work_index'})
        return selected
