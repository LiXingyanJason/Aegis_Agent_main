"""根任务图的稳定状态定义。"""

from typing import Any, TypedDict


class TaskGraphState(TypedDict, total=False):
    """跨工作流共享的最小状态；业务事实始终以数据库记录为准。"""

    run_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    history: list[dict[str, Any]]
    latest_user_message: str
    intent: str # 意图
    workflow: str
    selected_tool_name: str
    selected_tool_arguments: dict[str, Any]
    tool_context: str
    llm_step_id: str
    assistant_content: str
