"""用现有ToolHandler契约封装站内卡片读取。"""
from aegis_agent_worker.tool.contracts import ToolError, ToolResult


class CardContextToolHandler:
    def __init__(self, service):
        self._service = service
    async def invoke(self, context, arguments):
        arguments = dict(arguments)
        name = arguments.pop("__tool_name", None)
        if name not in {"conversation.cards.list", "conversation.cards.get"}:
            raise ToolError("TOOL_NOT_REGISTERED", "未知卡片上下文工具")
        payload = await self._service.read(context, name, arguments)
        # 不在SSE重发卡片标题。只记录来源与状态；原有Artifact接口仍负责前端展示。
        summary = {key: payload[key] for key in ("card_id", "query_id", "card_type", "status") if key in payload}
        summary["item_count"] = len(payload.get("items", payload.get("cards", [])))
        return ToolResult(payload, summary)
