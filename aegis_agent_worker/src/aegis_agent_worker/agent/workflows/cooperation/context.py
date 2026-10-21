"""显式选择团队及专家输入；不查SQL，不传播根图完整history或凭据。"""
from aegis_agent_worker.agent.contracts.cooperation.context import TeamContext
from aegis_agent_worker.agent.routing.team_registry import TeamValidationError


def team_context(state, policy):
    context = TeamContext(goal=state["cooperation_goal"], results=state["cooperation_results"])
    if len(context.model_dump_json().encode("utf-8")) > policy.max_context_bytes:
        raise TeamValidationError("COOPERATION_CONTEXT_TOO_LARGE")
    return context


def expert_input(state, context, delegation):
    # 只给当前专家每个领域最近一次反馈，不能广播整个委派历史。
    # 邮件专家只需邮件引用和待解决项；日历专家额外接收最近已核验的会议条件。
    if delegation.target not in {"mail", "calendar"}:
        raise TeamValidationError("COOPERATION_UNKNOWN_EXPERT")
    latest = {item.expert: index for index, item in enumerate(context.results)}
    selected = []
    for index, result in enumerate(context.results):
        if latest[result.expert] != index:
            continue
        data = result.model_dump()
        data.update(draft=None, references=tuple(ref for ref in result.references if ref.resource_type == "email"))
        if delegation.target == "mail":
            data["meeting_requirements"] = None
        selected.append(type(result).model_validate(data))
    context = TeamContext(goal=context.goal, results=selected)
    # 不传history、根图完整状态、审批决定或其他专家局部工具历史。
    return {**{key: state[key] for key in (
        "run_id", "tenant_id", "user_id", "conversation_id", "run_type", "execution_policy_version")},
        "delegation": delegation.model_dump(mode="json"),
        "team_context": context.model_dump(mode="json")}


def expert_feedback(context):
    """仅给领域模型安全反馈；不带其他专家身份凭据、草稿内容或原始工具历史。"""
    return [{"expert": item.expert, "status": item.status, "summary": item.summary,
             "error_code": item.error_code, "unresolved_fields": list(item.unresolved_fields)}
            for item in context.results]
