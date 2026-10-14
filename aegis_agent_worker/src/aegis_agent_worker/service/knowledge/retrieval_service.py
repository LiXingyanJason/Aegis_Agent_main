"""检索策略的最小接口；未配置真实Repository时必须明确拒绝。"""
from aegis_agent_worker.agent.contracts.knowledge import KnowledgeQueryPlan, KnowledgeSearchResult
from aegis_agent_worker.repository.knowledge_repository import KnowledgeRepository
from aegis_agent_worker.tool.contracts import ToolError


class RetrievalService:
    def __init__(self, repository: KnowledgeRepository | None = None):
        self._repository = repository

    async def search(self, context, plan):
        plan = KnowledgeQueryPlan.model_validate(plan.model_dump() if isinstance(plan, KnowledgeQueryPlan) else plan)
        if self._repository is None:
            raise ToolError("KNOWLEDGE_NOT_CONFIGURED", "知识库检索尚未配置，无法查询公司政策或项目资料")
        if context.tenant_id is None or context.user_id is None:
            raise ToolError("KNOWLEDGE_CONTEXT_REQUIRED", "缺少可信检索身份")
        try:
            rows = await self._repository.retrieve(context, plan)
            return KnowledgeSearchResult(evidence=list(rows)[:plan.top_k])
        except ValueError:
            raise ToolError("KNOWLEDGE_INVALID_EVIDENCE", "检索返回的证据未通过契约校验") from None
