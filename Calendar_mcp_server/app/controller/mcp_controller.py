"""MCP Streamable HTTP 接口层。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header

from app.config.dependencies import get_mcp_service
from app.config.settings import Settings, get_settings
from app.param.mcp_param import MCPRequestParam
from app.security.service_auth import validate_service_key
from app.service.mcp_service import MCPService

router = APIRouter(tags=["mcp"])


@router.post("/mcp")
async def handle_mcp_request(
    request: MCPRequestParam,
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
    service: MCPService = Depends(get_mcp_service),
) -> dict:
    """校验服务间认证后，将 JSON-RPC 请求交由 MCPService 分发。"""
    validate_service_key(authorization, settings.calendar_mcp_api_key)
    return await service.handle(request)
