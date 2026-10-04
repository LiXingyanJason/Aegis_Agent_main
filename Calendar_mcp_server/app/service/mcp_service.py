"""MCP 协议分发服务。"""

from pydantic import ValidationError

from app.common.constants import MCP_PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION, TOOL_FIND_FREE_TIME, TOOL_LIST_EVENTS
from app.common.exceptions import MCPError
from app.common.mcp_response import error, success
from app.param.calendar_param import FindFreeTimeParam, ListEventsParam
from app.param.mcp_param import MCPRequestParam
from app.security.aegis_context import parse_aegis_context
from app.service.calendar_service import CalendarService
from app.vo.tool_definition_vo import list_tool_definitions


class MCPService:
    """处理 MCP initialize、tools/list 与 tools/call 的协议分发。"""

    def __init__(self, calendar_service: CalendarService) -> None:
        self._calendar_service = calendar_service

    async def handle(self, request: MCPRequestParam) -> dict:
        """执行一次 JSON-RPC 请求，并始终返回协议格式的结果。"""
        request_id = request.id
        if request.jsonrpc != "2.0":
            return error(request_id, -32600, "仅支持 JSON-RPC 2.0")
        if request.method == "initialize":
            return success(request_id, {"protocolVersion": MCP_PROTOCOL_VERSION, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}, "capabilities": {"tools": {}}})
        if request.method == "tools/list":
            return success(request_id, {"tools": list_tool_definitions()})
        if request.method != "tools/call":
            return error(request_id, -32601, f"不支持的方法：{request.method}")
        try:
            content = await self._call_tool(request.params)
        except MCPError as exc:
            return error(request_id, exc.code, exc.message)
        return success(request_id, {"structuredContent": content, "content": [{"type": "text", "text": "Mock 日历查询完成。"}], "isError": False})

    async def _call_tool(self, params: dict | None) -> dict:
        """校验上下文、解析工具参数并调用日历业务服务。"""
        if not isinstance(params, dict):
            raise MCPError(-32602, "tools/call 缺少 params")
        tool_name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            raise MCPError(-32602, "工具参数或 Aegis 上下文无效")
        context = parse_aegis_context(params.get("_meta"))
        try:
            if tool_name == TOOL_LIST_EVENTS:
                value = await self._calendar_service.list_events(context, ListEventsParam.model_validate(arguments))
            elif tool_name == TOOL_FIND_FREE_TIME:
                value = await self._calendar_service.find_free_time(context, FindFreeTimeParam.model_validate(arguments))
            else:
                raise MCPError(-32601, f"未注册工具：{tool_name}")
        except ValidationError as exc:
            raise MCPError(-32602, str(exc)) from exc
        return value.model_dump(mode="json")
