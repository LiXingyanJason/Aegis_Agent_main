"""日历子图使用的局部状态。"""

from typing import Any, TypedDict


class CalendarReadState(TypedDict, total=False):
    """只读日历子图与根图共享的最小字段。"""

    run_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    latest_user_message: str
    selected_tool_name: str
    selected_tool_arguments: dict[str, Any]
    tool_context: str
