"""参会人契约对模型可见；姓名包裹/重复地址拒绝且可安全修复。"""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from aegis_agent_worker.agent.contracts.cooperation.mail import MailMeetingExtraction, MailMeetingFacts
from aegis_agent_worker.agent.contracts.input_choice import choice_id
from aegis_agent_worker.llm.output_validation import safe_output_issues
from test_mail_meeting_requirements import setup, initial, SEARCH, read, EXTRACT, RETURN, reference
from test_cooperation_runtime import RUN, DELEGATE_MAIL, DELEGATE_CALENDAR
from test_cooperation_integration import assemble, MAIN
from test_calendar_team_expert import QUERY, GENERATE, proposal


ADDRESSES = ["linyue@xinghe.example.com", "zhouchen@xinghe.example.com"]
INVITATION = "请邀请林悦（linyue@xinghe.example.com）及我方同事周晨（zhouchen@xinghe.example.com）。"


def extraction():
    result = deepcopy(EXTRACT)
    result["facts"]["attendees"] = ADDRESSES
    result["evidence_quotes"].append(INVITATION)
    return result


def test_attendee_schema_exposes_format_uniqueness_and_optional_semantics():
    schema = MailMeetingExtraction.model_json_schema()["$defs"]["MailMeetingFacts"]["properties"]["attendees"]
    assert schema["uniqueItems"] is True and schema["maxItems"] == 10
    assert schema["items"]["pattern"] and "纯邮箱" in schema["items"]["description"]
    assert MailMeetingFacts().attendees == ()
    assert MailMeetingFacts(attendees=[" LINYUE@XINGHE.EXAMPLE.COM "]).attendees == (ADDRESSES[0],)


@pytest.mark.parametrize("values,code", [
    (["林悦（linyue@xinghe.example.com）"], "invalid_attendee_email"),
    (["林悦 <linyue@xinghe.example.com>"], "invalid_attendee_email"),
    (["linyue@xinghe.example.com,zhouchen@xinghe.example.com"], "invalid_attendee_email"),
    (["PRIVATE_BAD_ADDRESS"], "invalid_attendee_email"),
    ([ADDRESSES[0], ADDRESSES[0].upper()], "duplicate_attendee_email"),
])
def test_bad_addresses_have_distinct_safe_codes(values, code):
    with pytest.raises(ValidationError) as caught:
        MailMeetingFacts(attendees=values)
    issues = safe_output_issues(caught.value)
    assert issues[0].type == code and issues[0].path == "attendees"
    assert not any(value in str(issues) for value in values)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_values,code", [
    (["林悦（linyue@xinghe.example.com）"], "invalid_attendee_email"),
    ([ADDRESSES[0], ADDRESSES[0].upper()], "duplicate_attendee_email"),
])
async def test_attendee_repair_has_explicit_schema_and_reason(bad_values, code):
    correct = extraction()
    bad = deepcopy(correct)
    bad["facts"]["attendees"] = bad_values
    env = setup([SEARCH, read(), bad, correct, RETURN])
    env.store.raw["provider-1"]["body_text"] += "\n"+INVITATION
    async with env.harness.scope(RUN):
        result = await env.workflow.build().ainvoke(initial())
    assert result["expert_result"]["status"] == "completed"
    assert result["expert_result"]["meeting_requirements"]["attendees"] == ADDRESSES
    assert code in env.client.calls[3][-1].content
    assert "facts.attendees" in env.client.calls[3][-1].content
    assert env.db.budgets[RUN.id]["model_calls"] == 5


@pytest.mark.asyncio
async def test_selection_extraction_calendar_draft_preserves_mock_a_attendees(monkeypatch):
    draft = proposal()
    draft["attendees"] = ADDRESSES
    app = assemble(monkeypatch, [MAIN, DELEGATE_MAIL, SEARCH, read(2), read(), extraction(), RETURN,
        DELEGATE_CALENDAR, QUERY, GENERATE, draft, dict(action="finish")], count=2)
    app.mail.store.raw["provider-1"]["body_text"] += "\n"+INVITATION
    assert (await app.orchestrator.execute(RUN.id)).status == "waiting_input"
    question = app.ready[-1]
    app.answers[question] = dict(selected_option_id=choice_id(question, reference()))
    result = await app.orchestrator.execute(RUN.id, resume_input_id=question)
    assert result.status == "waiting_confirmation", app.lifecycle.failed
    saved = next(iter(app.env.repository.rows.values()))
    assert list(saved["attendees"]) == ADDRESSES
    assert app.env.repository.inserts == 1 and not app.writes
    assert len(app.audit) == 1
    calls = len(app.env.client.calls)
    result = await app.orchestrator.execute(RUN.id, resume_decision="approved")
    assert result.status == "completed" and len(app.writes) == 1
    assert len(app.env.client.calls) == calls
