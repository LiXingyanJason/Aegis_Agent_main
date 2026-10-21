"""真实冒烟发现的动作互斥/补问恢复/日历格式修复回归。"""
import pytest
from pydantic import ValidationError

from aegis_agent_worker.agent.contracts.cooperation.mail import MailMeetingDecision
from aegis_agent_worker.agent.contracts.cooperation.calendar import CalendarTeamDecision
from aegis_agent_worker.llm.output_validation import safe_output_issues
from test_calendar_team_expert import setup, initial, QUERY, GENERATE, proposal
from test_cooperation_runtime import RUN


@pytest.mark.parametrize("model,bad", [
    (MailMeetingDecision, dict(action="return_requirements", question="无关问题")),
    (MailMeetingDecision, dict(action="read_mail")),
    (CalendarTeamDecision, dict(action="generate_draft", window_index=0)),
    (CalendarTeamDecision, dict(action="finish_unavailable", question="无关问题")),
])
def test_action_combinations_are_declared_and_rejected_consistently(model, bad):
    branches = model.model_json_schema()["oneOf"]
    assert any(branch["properties"]["action"]["const"] == bad["action"] for branch in branches)
    with pytest.raises(ValidationError) as caught:
        model.model_validate(bad)
    assert safe_output_issues(caught.value)[0].type == "action_parameter_mismatch"


@pytest.mark.asyncio
async def test_calendar_invalid_decision_repairs_with_safe_action_reason_and_json_mode():
    env = setup([dict(action="generate_draft", window_index=0), QUERY, GENERATE, proposal()])
    async with env.harness.scope(RUN):
        result = await env.workflow.build().ainvoke(initial())
    assert result["expert_result"]["status"] == "completed"
    assert "action_parameter_mismatch" in env.client.calls[1][-1].content
    assert "complete_json" not in str(result)  # 通信细节不进入业务State。
    assert env.db.budgets[RUN.id]["model_calls"] == 4


@pytest.mark.asyncio
async def test_calendar_failed_decision_has_safe_stage_and_actual_repairs(caplog):
    bad = dict(action="PRIVATE_FAKE_ACTION")
    env = setup([bad, bad])
    async with env.harness.scope(RUN):
        result = await env.workflow.build().ainvoke(initial())
    output = result["expert_result"]
    assert output["error_code"] == "CALENDAR_TEAM_OUTPUT_INVALID"
    assert output["output_stage"] == "calendar_team_planning" and output["repair_count"] == 1
    assert output["validation_issues"] == [{"path":"action", "type":"literal_error"}]
    assert "PRIVATE_FAKE_ACTION" not in caplog.text
    assert not env.repository.rows and not env.provider.calls


@pytest.mark.asyncio
async def test_calendar_failed_draft_has_separate_diagnostic_stage():
    bad = {**proposal(), "PRIVATE_FIELD": "PRIVATE_VALUE"}
    env = setup([QUERY, GENERATE, bad, bad])
    async with env.harness.scope(RUN):
        result = await env.workflow.build().ainvoke(initial())
    output = result["expert_result"]
    assert output["error_code"] == "CALENDAR_TEAM_DRAFT_INVALID"
    assert output["output_stage"] == "calendar_team_draft" and output["repair_count"] == 1
    assert {"path":"field", "type":"extra_forbidden"} in output["validation_issues"]
    assert "PRIVATE" not in str(output) and not env.repository.rows
