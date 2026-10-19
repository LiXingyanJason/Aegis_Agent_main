"""仓库内真实Mock邮件与8.1时间核验契约保持一致；不调用模型或MCP。"""
import json
from pathlib import Path
import re
import pytest
from aegis_agent_worker.agent.contracts.calendar import CalendarDecision
from aegis_agent_worker.agent.contracts.cooperation import verify_grounded_calendar


def mailbox():
    path=Path(__file__).resolve().parents[3]/'Email_mcp_server/data/mock_mailbox.json'
    return {item['provider_message_id']:item for item in json.loads(path.read_text(encoding='utf-8'))}


@pytest.mark.parametrize('suffix,date,start,end,duration',[
    ('a-001','2026-10-26','09:00','17:00',50),
    ('b-002','2026-10-27','14:00','17:00',30),
    ('d-004','2026-10-29','09:00','12:00',45),
    ('d-005','2026-10-30','14:00','17:00',45),
    ('f-007','2026-10-28','19:00','20:00',30),
    ('g-008','2026-10-29','09:00','17:00',30),
])
def test_absolute_window_mail_has_code_verifiable_time_evidence(suffix,date,start,end,duration):
    item=mailbox()['mock-mail-coop-'+suffix]
    excerpt=next(line for line in item['body_text'].splitlines() if line.startswith('请在'))
    decision=CalendarDecision(action='prepare_meeting',time_mode='search_window',timezone='Asia/Shanghai',
        start_at=f'{date}T{start}:00+08:00',end_at=f'{date}T{end}:00+08:00',duration_minutes=duration)
    assert verify_grounded_calendar(decision,excerpt,'请根据所选邮件的时间要求生成会议草稿。')==decision


def test_missing_duration_and_relative_date_are_not_silent_success_fixtures():
    items=mailbox()
    missing=items['mock-mail-coop-c-003']['body_text']
    relative=items['mock-mail-coop-e-006']['body_text']
    decision=CalendarDecision(action='prepare_meeting',time_mode='search_window',timezone='Asia/Shanghai',
        start_at='2026-10-26T09:00:00+08:00',end_at='2026-10-26T17:00:00+08:00',duration_minutes=40)
    assert not re.search(r'\d+\s*分钟',missing)
    with pytest.raises(ValueError): verify_grounded_calendar(decision,missing,'按照邮件生成草稿。')
    with pytest.raises(ValueError): verify_grounded_calendar(decision,relative,'按照邮件生成草稿。')
