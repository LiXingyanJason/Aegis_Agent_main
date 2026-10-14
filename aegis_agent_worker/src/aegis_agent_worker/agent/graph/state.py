"""根任务图的稳定状态定义。"""

from typing import Any, TypedDict


class TaskGraphState(TypedDict, total=False):
    """跨工作流共享的最小状态；业务事实始终以数据库记录为准。"""

    run_id: str
    tenant_id: str
    user_id: str
    conversation_id: str
    run_type: str
    execution_policy_version: str  # 代码固定的策略包版本，模型不可填写
    history: list[dict[str, Any]]
    latest_user_message: str
    memory_context: str
    intent: str # 意图
    workflow: str
    main_decision: dict[str, Any]
    reply_context: list[dict[str, Any]]  # 仅只读卡片安全结果，不能证明外部业务已执行
    handoff: dict[str, Any]
    knowledge_plan: dict[str, Any]
    knowledge_evidence: list[dict[str, Any]]
    knowledge_answer: dict[str, Any]
    knowledge_error: str
    knowledge_error_code: str
    knowledge_status: str
    # 领域 Agent 产生、且已经过 Pydantic 校验的受限执行计划。
    plan: dict[str, Any]
    # 工具调用的脱敏结果摘要；完整事实仍在 tool_calls 等业务表。
    tool_result: dict[str, Any]
    # 供前端卡片及短期指代使用的稳定对象引用和展示字段。
    artifacts: list[dict[str, Any]]
    selected_tool_name: str
    selected_tool_arguments: dict[str, Any]
    tool_context: str
    # 会议安排确认图：事实候选与模型草稿分开保存，恢复后仍可重新校验。
    calendar_meeting_plan: dict[str, Any]
    calendar_decision: dict[str, Any]
    calendar_planning_error: str
    availability: dict[str, Any]
    calendar_meeting_draft: dict[str, Any]
    awaiting_confirmation: bool
    draft_id: str
    approval_item_id: str
    approval_decision: str
    confirmation_error: str
    llm_step_id: str
    assistant_content: str
    mail_workflow_stage: str
    mail_query_error: str
    mail_decision: dict[str, Any]
    mail_planning_error: str
    collection_decision: dict[str, Any]
    collected_evidence: list[dict[str, Any]]
    user_answers: list[str]
    collection_round: int
    waiting_input: bool
    input_request_id: str
    input_request: dict[str, Any]
    mail_workflow_completed: bool
