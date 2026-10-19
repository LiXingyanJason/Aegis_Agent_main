"""7.3只读接入：Worker没有创建记忆能力，模型前只读当前有效记录。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from aegis_agent_worker.agent.graph.task_graph import TaskGraph,TaskGraphDependencies
from aegis_agent_worker.repository.memory_repository import MemoryRepository
from aegis_agent_worker.service.memory.memory_context_service import _select_relevant,_terms,MemoryContextService


@pytest.mark.asyncio
async def test_worker_sql_is_read_only_and_requires_all_lifecycle_facts():
    execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda:SimpleNamespace(all=lambda:[])))
    await MemoryRepository().list_active_non_sensitive(SimpleNamespace(execute=execute),tenant_id=UUID(int=1),user_id=UUID(int=2))
    query=str(execute.await_args.args[0])
    for clause in ("status='confirmed'","sensitivity='normal'",'confirmed_at IS NOT NULL','expires_at>now()',
                   'aegis_memory_source_accessible','deleted_at IS NULL','user_id=:user_id','tenant_id=:tenant_id'):
        assert clause in query
    assert not hasattr(MemoryRepository(),'create_memory') and not hasattr(MemoryRepository(),'update_memory')


@pytest.mark.asyncio
async def test_final_reply_ignores_checkpoint_memory_and_reloads_before_model():
    client=SimpleNamespace(provider_name='mock',model_name='mock',complete=AsyncMock(return_value='回答'))
    memory=SimpleNamespace(select_for_run=AsyncMock(return_value=[]),format_system_context=MemoryContextService.format_system_context)
    graph=TaskGraph(TaskGraphDependencies(context_service=None,lifecycle=SimpleNamespace(start_llm_generation=AsyncMock(return_value=UUID(int=9))),
        llm_client=client,system_prompt='系统边界',workflow_factories=(),memory_context_service=memory))
    state={key:str(UUID(int=i)) for i,key in enumerate(('run_id','tenant_id','user_id','conversation_id'),1)}
    state.update(memory_context='旧版本已删除偏好不得复用',latest_user_message='你好',history=[{'role':'user','content':'你好'}])
    await graph._generate_reply(state)
    memory.select_for_run.assert_awaited_once()
    assert all('旧版本已删除偏好' not in message.content for message in client.complete.await_args.args[0])


def test_entity_fields_not_description_drive_selected_value():
    rows=[{'id':UUID(int=1),'content':'错误说明：使用美国时间','source':'user_confirmed','memory_type':'entity',
           'entity_value':{'key':'timezone','value':'Asia/Shanghai'}},
          {'id':UUID(int=2),'content':'错误说明：用简洁语气','source':'user_confirmed','memory_type':'entity',
           'entity_value':{'key':'reply_style','value':'formal'}}]
    calendar=_select_relevant(rows,_terms('安排会议'))
    mail=_select_relevant(rows,_terms('起草邮件回复'))
    assert len(calendar)==1 and 'Asia/Shanghai' in calendar[0].content and '美国' not in calendar[0].content
    assert len(mail)==1 and 'formal' in mail[0].content and '简洁' not in mail[0].content


def test_overlong_entity_is_not_truncated_into_partial_email():
    record={'id':UUID(int=1),'content':'联系人','source':'user_confirmed','memory_type':'entity',
            'entity_value':{'key':'contact','name':'李莉'*50,'email':'x'*250+'@example.com'}}
    assert _select_relevant([record],_terms('给李莉回复邮件'))==[]


@pytest.mark.asyncio
async def test_mail_format_repair_reloads_memory_instead_of_reusing_revoked_excerpt():
    from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
    client=SimpleNamespace(complete=AsyncMock(side_effect=['not json','{"to":["a@example.com"],"cc":[],"subject":"回复","body":"正文"}']))
    memory=AsyncMock(side_effect=['本次有效的偏好',None])
    await MailAgent(client).generate_reply_draft({'sender':'a@example.com','subject':'原信'},
        key_points=[],instruction='起草',memory_context_loader=memory)
    assert memory.await_count==2
    assert any('本次有效的偏好' in message.content for message in client.complete.await_args_list[0].args[0])
    assert all('本次有效的偏好' not in message.content for message in client.complete.await_args_list[1].args[0])
