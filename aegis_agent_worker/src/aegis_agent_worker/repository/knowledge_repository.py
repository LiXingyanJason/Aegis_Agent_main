"""证据复核边界；旧单路retrieve接口仅供显式适配/替身，不承担Hybrid编排。"""
from typing import Protocol, Sequence
from aegis_agent_worker.agent.contracts.knowledge import KnowledgeQueryPlan, RetrievedEvidence
from aegis_agent_worker.tool.contracts import ToolContext


class KnowledgeEvidenceReader(Protocol):
    async def revalidate(self, context: ToolContext, evidence: Sequence[RetrievedEvidence]) -> Sequence[RetrievedEvidence]:
        """按稳定引用复核当前权限、有效版本及内容hash，不信任模型声明。"""
        ...


class KnowledgeRepository(KnowledgeEvidenceReader, Protocol):
    async def retrieve(self, context: ToolContext, plan: KnowledgeQueryPlan) -> Sequence[RetrievedEvidence]:
        """在数据库侧按可信身份、ACL与有效版本过滤后返回候选。"""
        ...
