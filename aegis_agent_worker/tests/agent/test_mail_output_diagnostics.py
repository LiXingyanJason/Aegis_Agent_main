"""安全诊断随邮件失败结果流转；修复不回传非法输出且不扩预算。"""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from aegis_agent_worker.agent.contracts.output_validation import OutputValidationIssue
from aegis_agent_worker.agent.contracts.cooperation.messages import ExpertResult
from aegis_agent_worker.agent.contracts.cooperation.mail import MailMeetingExtraction
from aegis_agent_worker.llm.output_validation import safe_output_issues
from aegis_agent_worker.service.cooperation.team_event_service import TeamEventService
from test_mail_meeting_requirements import setup, initial, SEARCH, read, EXTRACT, RETURN, FACTS
from test_cooperation_runtime import RUN
from test_cooperation_events import EventsDatabase, Runs


@pytest.mark.asyncio
async def test_extraction_invalid_has_safe_stage_schema_path_and_repair_count(caplog):
    bad = deepcopy(EXTRACT)
    bad["facts"]["duration_minutes"] = "PRIVATE_INVALID_DURATION"
    bad["PRIVATE_FIELD_NAME"] = "PRIVATE_VALUE"
    env = setup([SEARCH, read(), bad, bad])
    async with env.harness.scope(RUN):
        output = await env.workflow.build().ainvoke(initial())
    result = output["expert_result"]
    assert result["error_code"] == "MODEL_OUTPUT_INVALID"
    assert result["output_stage"] == "mail_meeting_extraction" and result["repair_count"] == 1
    assert {"path": "facts.duration_minutes", "type": "int_type"} in result["validation_issues"]
    repair = env.client.calls[-1][-1].content
    assert "facts.duration_minutes" in repair and "int_type" in repair
    for private in ("PRIVATE_INVALID_DURATION", "PRIVATE_FIELD_NAME", "PRIVATE_VALUE"):
        assert private not in repair and private not in caplog.text and private not in str(result)
    assert env.db.budgets[RUN.id]["model_calls"] == 4
    assert len(env.store.tool_calls) == 2
    db = EventsDatabase()
    await TeamEventService(db, Runs()).record(RUN, phase="expert_completed", round_no=1,
        policy_version=output["execution_policy_version"], result=ExpertResult.model_validate(result))
    assert db.rows[0].payload["output_stage"] == "mail_meeting_extraction"
    assert db.rows[0].payload["validation_issues"] == result["validation_issues"]


@pytest.mark.asyncio
async def test_planning_invalid_json_is_distinct_from_extraction(caplog):
    env = setup(["PRIVATE_NOT_JSON", "PRIVATE_NOT_JSON"])
    async with env.harness.scope(RUN):
        output = await env.workflow.build().ainvoke(initial())
    result = output["expert_result"]
    assert result["output_stage"] == "mail_meeting_planning"
    assert result["validation_issues"] == [{"path": "", "type": "invalid_json"}]
    assert not env.store.tool_calls and len(env.client.calls) == 2
    assert "PRIVATE_NOT_JSON" not in caplog.text


@pytest.mark.asyncio
async def test_evidence_mismatch_repair_gets_reason_without_quote(caplog):
    bad = dict(facts=FACTS, evidence_quotes=["PRIVATE_FABRICATED_QUOTE"])
    env = setup([SEARCH, read(), bad, EXTRACT, RETURN])
    async with env.harness.scope(RUN):
        output = await env.workflow.build().ainvoke(initial())
    assert output["expert_result"]["status"] == "completed"
    repair = env.client.calls[3][-1].content
    assert "evidence_quotes" in repair and "evidence_quote_mismatch" in repair
    assert "PRIVATE_FABRICATED_QUOTE" not in repair and "PRIVATE_FABRICATED_QUOTE" not in caplog.text


def test_nested_unknown_path_and_context_never_leak():
    with pytest.raises(ValidationError) as caught:
        MailMeetingExtraction.model_validate(dict(facts={**FACTS, "PRIVATE_FIELD": "PRIVATE_VALUE"}))
    issues = [item.model_dump() for item in safe_output_issues(caught.value)]
    assert {"path": "facts.field", "type": "extra_forbidden"} in issues
    assert "PRIVATE" not in json.dumps(issues)
    with pytest.raises(ValueError):
        OutputValidationIssue(path="PRIVATE_FIELD", type="invalid_output")
    with pytest.raises(ValueError):
        OutputValidationIssue(path="facts", type="PRIVATE_TYPE")


@pytest.mark.asyncio
async def test_evidence_failure_exposes_business_reason_without_weakening_validation():
    bad = dict(facts=FACTS, evidence_quotes=[])
    env = setup([SEARCH, read(), bad, bad])
    async with env.harness.scope(RUN):
        output = await env.workflow.build().ainvoke(initial())
    assert output["expert_result"]["validation_issues"] == [
        {"path": "evidence_quotes", "type": "evidence_required"}]
    assert output["expert_result"]["meeting_requirements"] is None


def test_old_expert_result_without_diagnostics_is_compatible():
    result = ExpertResult(delegation_id=initial()["delegation"]["delegation_id"], expert="mail",
        status="unavailable", summary="旧任务", error_code="MODEL_OUTPUT_INVALID")
    assert result.output_stage is None and not result.validation_issues
    with pytest.raises(ValueError):
        ExpertResult.model_validate({**result.model_dump(), "output_stage": "mail_meeting_extraction"})
