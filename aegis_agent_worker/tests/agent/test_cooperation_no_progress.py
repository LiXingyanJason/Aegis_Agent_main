"""失败目标交接、零结果止损与无新信息重新委派的回归。"""
from copy import deepcopy
from dataclasses import replace

import pytest

from aegis_agent_worker.agent.contracts.cooperation.messages import DelegationMessage, ExpertResult
from aegis_agent_worker.agent.contracts.decision import ResourceReference
from aegis_agent_worker.service.cooperation.team_event_service import TeamEventService
from test_mail_meeting_requirements import setup, initial, SEARCH, read, EXTRACT, RETURN, reference
from test_cooperation_runtime import RUN, DELEGATE_MAIL, setup as team_setup, state, Expert
from test_cooperation_events import EventsDatabase, Runs
from aegis_agent_worker.harness.policy_provider import COOPERATION_MAIL_POLICY


@pytest.mark.asyncio
async def test_failed_planning_preserves_selected_source_and_redelegation_cannot_search():
    env = setup([SEARCH, read(), EXTRACT, dict(action="finish_unavailable")])
    async with env.harness.scope(RUN):
        first = await env.workflow.build().ainvoke(initial())
    result = first["expert_result"]
    assert result["references"] == [ResourceReference.model_validate(reference()).model_dump(mode="json")]
    assert result["meeting_requirements"] is None
    delegation = DelegationMessage.model_validate(initial()["delegation"])
    assert await env.sources.verify_result(RUN, delegation, result)
    # 新委派从已验证引用重建候选，不继承旧facts或正文；禁止重新搜索替换对象。
    retry = setup([SEARCH])
    async with retry.harness.scope(RUN):
        output = await retry.workflow.build().ainvoke(initial([result]))
    assert output["mail_meeting_selection_locked"]
    assert output["expert_result"]["error_code"] == "MAIL_SELECTION_MISMATCH"
    assert not retry.store.tool_calls
    # 合法重入仍可重读所选来源并完成；不复用先前提取事实。
    retry = setup([read(), EXTRACT, RETURN])
    async with retry.harness.scope(RUN):
        output = await retry.workflow.build().ainvoke(initial([result]))
    assert output["expert_result"]["status"] == "completed"
    assert all(arguments["__tool_name"] == "mail.messages.get" for _, arguments in retry.store.tool_calls)
    env.store.active = False
    assert not await env.sources.verify_result(RUN, delegation, result)
    async with env.harness.scope(RUN):
        prepared = await env.workflow._prepare(initial([result]))
    assert prepared["expert_result"]["status"] == "unavailable"
    assert not prepared["expert_result"]["references"]


@pytest.mark.asyncio
async def test_different_empty_searches_stop_at_policy_limit():
    second = deepcopy(SEARCH)
    second["query"]["keywords"] = ["合同会议"]
    env = setup([SEARCH, second], count=0)
    async with env.harness.scope(RUN):
        output = await env.workflow.build().ainvoke(initial())
    assert output["expert_result"]["error_code"] == "MAIL_MEETING_SEARCH_NO_PROGRESS"
    assert len(env.store.tool_calls) == 2
    assert output["mail_meeting_empty_searches"] == env.provider.current(output).mail_meeting.max_empty_searches


@pytest.mark.asyncio
async def test_empty_search_limit_is_policy_driven():
    profile = replace(COOPERATION_MAIL_POLICY,
        mail_meeting=replace(COOPERATION_MAIL_POLICY.mail_meeting, max_empty_searches=1))
    env = setup([SEARCH], count=0, profile=profile)
    async with env.harness.scope(RUN):
        output = await env.workflow.build().ainvoke(initial())
    assert output["expert_result"]["error_code"] == "MAIL_MEETING_SEARCH_NO_PROGRESS"
    assert len(env.store.tool_calls) == 1


@pytest.mark.asyncio
async def test_no_new_information_cannot_redelegate_failed_expert():
    async def unavailable(input_state):
        return dict(status="unavailable", summary="缺少来源", references=[], error_code="MAIL_MEETING_SEARCH_NO_PROGRESS")
    workflow, harness, client, db, events, mail, calendar = team_setup(
        [DELEGATE_MAIL, DELEGATE_MAIL], mail=Expert("mail", unavailable))
    async with harness.scope(RUN):
        result = await workflow.build().ainvoke(state())
    assert result["cooperation_status"] == "unavailable"
    assert result["cooperation_error_code"] == "MAIL_MEETING_SEARCH_NO_PROGRESS"
    assert len(mail.calls) == 1 and len(result["cooperation_delegations"]) == 1


@pytest.mark.asyncio
async def test_completed_event_exposes_safe_failure_code_and_missing_fields():
    db = EventsDatabase()
    result = ExpertResult(delegation_id=initial()["delegation"]["delegation_id"], expert="mail",
        status="unavailable", summary="此自由文本不应进入事件", error_code="MODEL_OUTPUT_INVALID",
        unresolved_fields=("time_windows",))
    await TeamEventService(db, Runs()).record(RUN, phase="expert_completed", round_no=1,
        policy_version="harness-v5-team-calendar", result=result)
    payload = db.rows[0].payload
    assert payload["error_code"] == "MODEL_OUTPUT_INVALID"
    assert payload["unresolved_fields"] == ["time_windows"]
    assert "summary" not in payload and "此自由文本" not in str(payload)
