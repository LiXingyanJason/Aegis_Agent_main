"""knowledge.search契约适配，不决定检索算法或用户权限。"""
from aegis_agent_worker.agent.contracts.knowledge import KnowledgeQueryPlan
from aegis_agent_worker.tool.contracts import ToolError, ToolResult


class KnowledgeSearchHandler:
    def __init__(self, retrieval_service):
        self._retrieval = retrieval_service

    async def invoke(self, context, arguments):
        if arguments.get("__tool_name") != "knowledge.search":
            raise ToolError("TOOL_SCOPE_BLOCKED", "知识处理器只支持受控只读检索")
        values = {key: value for key, value in arguments.items() if key != "__tool_name"}
        try:
            plan = KnowledgeQueryPlan.model_validate(values)
        except ValueError:
            raise ToolError("TOOL_INVALID_ARGUMENTS", "知识查询参数未通过校验") from None
        result = await self._retrieval.search(context, plan)
        return ToolResult(response_payload=result.model_dump(mode="json"), output_summary={
            "evidence_count": len(result.evidence),
            "sources": [{"document_id": str(item.document_id), "version_id": str(item.version_id),
                         "chunk_id": str(item.chunk_id)} for item in result.evidence],
        })
