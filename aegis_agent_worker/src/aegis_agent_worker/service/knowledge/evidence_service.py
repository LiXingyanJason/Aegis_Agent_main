"""版本／权限事实由Repository核验，不由LLM认定。"""
from aegis_agent_worker.agent.contracts.knowledge import KnowledgeSearchResult
from aegis_agent_worker.repository.knowledge_repository import KnowledgeEvidenceReader
from aegis_agent_worker.tool.contracts import ToolError


class EvidenceService:
    def __init__(self, repository: KnowledgeEvidenceReader | None = None):
        self._repository = repository

    async def verify(self, context, evidence):
        if self._repository is None:
            raise ToolError("KNOWLEDGE_NOT_CONFIGURED", "知识库证据复核尚未配置")
        try:
            original = KnowledgeSearchResult(evidence=evidence)
            current = KnowledgeSearchResult(evidence=list(await self._repository.revalidate(context, original.evidence)))
            prior = {item.chunk_id: item for item in original.evidence}
            # 复核不得用新版本／新文本悄悄替代原证据，撤权后直接删除该证据。
            accepted = {item.chunk_id:item for item in current.evidence if prior.get(item.chunk_id) == item}
            return [accepted[item.chunk_id] for item in original.evidence if item.chunk_id in accepted]
        except ValueError:
            raise ToolError("KNOWLEDGE_INVALID_EVIDENCE", "来源复核结果无效") from None
