"""MCP tools/list 返回的工具定义。"""

from typing import Any

from app.common.constants import TOOL_FIND_FREE_TIME, TOOL_LIST_EVENTS


def list_tool_definitions() -> list[dict[str, Any]]:
    """返回两个可发现的只读日历工具 Schema。"""
    common = {
        "type": "object",
        "required": ["start_at", "end_at"],
        "properties": {
            "start_at": {"type": "string", "format": "date-time"},
            "end_at": {"type": "string", "format": "date-time"},
        },
    }
    return [
        {
            "name": TOOL_LIST_EVENTS,
            "description": "查询指定时间范围内当前用户的日程。仅只读。",
            "inputSchema": common,
        },
        {
            "name": TOOL_FIND_FREE_TIME,
            "description": "查询指定时间范围内可容纳会议时长的空闲时段。仅只读。",
            "inputSchema": {
                **common,
                "properties": {
                    **common["properties"],
                    "duration_minutes": {"type": "integer", "minimum": 15, "maximum": 480},
                    "participants": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                },
            },
        },
    ]
