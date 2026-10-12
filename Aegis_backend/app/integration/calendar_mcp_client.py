"""日历页面使用的只读 MCP 客户端。"""

import asyncio
from typing import Any
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from app.config.settings import Settings


class CalendarMCPError(RuntimeError):
    """配置、网络或 MCP 响应异常。"""


class CalendarMCPEvent(BaseModel):
    external_event_id: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=500)
    start_at: AwareDatetime
    end_at: AwareDatetime
    attendees: list[str]
    description: str | None = None
    location: str | None = None

    @model_validator(mode="after")
    def validate_interval(self):
        """外部日程必须包含有效的带时区起止时间。"""
        if self.end_at <= self.start_at:
            raise ValueError("日程起止时间无效")
        return self


class CalendarMCPList(BaseModel):
    source: str = Field(min_length=1)
    events: list[CalendarMCPEvent]

    @model_validator(mode="after")
    def unique_events(self):
        """重复标识会破坏页面选中关系，应作为工具错误处理。"""
        ids = [item.external_event_id for item in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("日程标识重复")
        return self


class CalendarMCPDetail(BaseModel):
    source: str = Field(min_length=1)
    event: CalendarMCPEvent | None


class CalendarMCPClient:
    def __init__(self, settings: Settings, http_client_factory=None):
        self._settings = settings
        self._http_client_factory = http_client_factory

    async def call(self, tool_name: str, arguments: dict, *, connection_id, tenant_id, user_id) -> dict[str, Any]:
        """以独立请求标识调用读取工具，不为页面查询伪造 Agent run。"""
        if tool_name not in {"calendar.list_events", "calendar.get_event"}:
            raise CalendarMCPError("页面不允许调用写入工具")
        if self._settings.calendar_mcp_url is None:
            raise CalendarMCPError("日历工具服务尚未配置")
        headers = {"Accept": "application/json, text/event-stream"}
        if self._settings.calendar_mcp_api_key:
            headers["Authorization"] = f"Bearer {self._settings.calendar_mcp_api_key.get_secret_value()}"
        timeout = self._settings.calendar_mcp_timeout_seconds
        meta = {
            "aegis_connection_id": str(connection_id), "aegis_tenant_id": str(tenant_id),
            "aegis_user_id": str(user_id), "aegis_request_id": str(uuid4()),
        }
        try:
            async with asyncio.timeout(timeout):
                client = (self._http_client_factory(headers, timeout) if self._http_client_factory
                          else httpx.AsyncClient(headers=headers, timeout=timeout))
                async with client:
                    async with streamable_http_client(str(self._settings.calendar_mcp_url), http_client=client) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool(tool_name, arguments, meta=meta)
            if result.is_error or not isinstance(result.structured_content, dict):
                raise CalendarMCPError("日历工具返回无效结果")
            return result.structured_content
        except Exception as error:
            raise CalendarMCPError("日历工具服务暂时不可用或响应无效") from error
