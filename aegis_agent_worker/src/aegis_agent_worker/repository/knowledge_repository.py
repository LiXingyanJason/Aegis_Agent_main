"""知识库Repository边界；真实SQL及RLS在6.2～6.4接入，不提供虚构生产实现。"""
from typing import Protocol, Sequence
from aegis_agent_worker.agent.contracts.knowledge import KnowledgeQueryPlan, RetrievedEvidence
from aegis_agent_worker.tool.contracts import ToolContext


class KnowledgeRepository(Protocol):
    async def retrieve(self, context: ToolContext, plan: KnowledgeQueryPlan) -> Sequence[RetrievedEvidence]:
        """在数据库侧按可信身份、ACL与有效版本过滤后返回候选。"""
        ...

    async def revalidate(self, context: ToolContext, evidence: Sequence[RetrievedEvidence]) -> Sequence[RetrievedEvidence]:
        """按稳定引用复核当前权限、有效版本及内容hash，不信任模型声明。"""
        ...
