"""7.5固定确定性评测：不编造线上准确率，不调用模型判断权限或冲突。"""
from datetime import datetime,timezone
from uuid import UUID
from dataclasses import replace
import json

import pytest
from test_memory_call_injection import setup,row
from aegis_agent_worker.harness.policy_provider import LEGACY_POLICY
from aegis_agent_worker.repository.memory_repository import MemoryRepository
from unittest.mock import AsyncMock
from types import SimpleNamespace


def summary(index,content,year=2026,**metadata):
    return {**row(content), 'id':UUID(int=index),'updated_at':datetime(year,1,1,tzinfo=timezone.utc),**metadata}


def entity(index,key,value):
    return {**summary(index,'不使用此说明解释实体'), 'memory_type':'entity','entity_value':{'key':key,'value':value}}


CASES=[
    ('older_strong_before_recent_weak',[summary(1,'邮件回复语气应保持正式',2020),summary(2,'邮件会议安排',2026)], [1], 'item_budget'),
    ('entity_before_summary',[entity(1,'reply_style','formal'),summary(2,'邮件语气可以随意一些')],[1],'entity_preferred'),
    ('summary_conflict',[summary(1,'邮件语气正式'),summary(2,'邮件语气随意')],[],'summary_conflict'),
    ('duplicate',[summary(1,'邮件语气正式',2020),summary(2,'邮件语气正式')],[2],'duplicate_memory'),
    ('ambiguous_with_entity',[entity(1,'reply_style','formal'),summary(2,'邮件项目事项')],[1],'ambiguous_summary'),
    ('sql_detected_hidden_conflict',[summary(1,'邮件语气正式',summary_conflicted=True)],[],'summary_conflict'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('name,rows,expected,reason',CASES,ids=[case[0] for case in CASES])
async def test_fixed_priority_cases(name,rows,expected,reason):
    profile=replace(LEGACY_POLICY,memory=replace(LEGACY_POLICY.memory,max_items=1)) if name.startswith('older') else LEGACY_POLICY
    run,service,_,_,memories,_=setup(profile)
    memories.rows=rows
    first,_=await service.prepare_call(run,purpose='mail_reply_draft',task_text='邮件回复语气',remaining_chars=10000)
    memories.rows=list(reversed(rows))
    second,_=await service.prepare_call(run,purpose='mail_reply_draft',task_text='邮件回复语气',remaining_chars=10000)
    assert [item.memory_id.int for item in first.items]==expected
    assert first.items==second.items
    assert first.selection_report.excluded_counts[reason]>0
    audit=json.dumps(first.selection_report.model_dump(mode='json'),ensure_ascii=False)
    assert '邮件语气' not in audit and '邮件项目' not in audit


@pytest.mark.asyncio
async def test_contact_requires_actual_name_or_address_in_task():
    run,service,_,_,memories,_=setup()
    memories.rows=[{**summary(1,'李莉'), 'memory_type':'entity','entity_value':{'key':'contact','name':'李莉','email':'lili@example.com'}}]
    absent,_=await service.prepare_call(run,purpose='mail_reply_draft',task_text='回复邮件',remaining_chars=10000)
    present,_=await service.prepare_call(run,purpose='mail_reply_draft',task_text='给李莉回复邮件',remaining_chars=10000)
    assert not absent.items and len(present.items)==1


@pytest.mark.asyncio
async def test_recall_sql_permissions_precede_matching_and_limit_is_after_ranking():
    execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda:SimpleNamespace(all=lambda:[])))
    from aegis_agent_worker.service.memory.selection import recall_hints
    policy=LEGACY_POLICY.memory
    await MemoryRepository().recall_candidates(SimpleNamespace(execute=execute),tenant_id=UUID(int=1),user_id=UUID(int=2),
        hints=recall_hints('mail_reply_draft','邮件回复',policy),policy=policy)
    sql=str(execute.await_args.args[0])
    for predicate in ('tenant_id=:tenant_id','user_id=:user_id',"status='confirmed'","sensitivity='normal'",
        'expires_at>now()','aegis_memory_source_accessible','WITH eligible AS MATERIALIZED'):
        assert predicate in sql
    assert sql.index('aegis_memory_source_accessible')<sql.index('), scored AS')<sql.index('ORDER BY')<sql.index('LIMIT :limit')
    assert '邮件回复' not in sql
