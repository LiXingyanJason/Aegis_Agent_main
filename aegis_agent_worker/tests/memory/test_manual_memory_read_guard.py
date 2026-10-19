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
async def test_final_reply_ignores_checkpoint_memory_and_declares_call_scope():
    from aegis_agent_worker.llm.memory_scope import current_memory_request
    scopes=[]
    async def complete(messages):
        scopes.append(current_memory_request())
        return '回答'
    client=SimpleNamespace(provider_name='mock',model_name='mock',complete=AsyncMock(side_effect=complete))
    memory=object()
    graph=TaskGraph(TaskGraphDependencies(context_service=None,lifecycle=SimpleNamespace(start_llm_generation=AsyncMock(return_value=UUID(int=9))),
        llm_client=client,system_prompt='系统边界',workflow_factories=(),memory_context_service=memory))
    state={key:str(UUID(int=i)) for i,key in enumerate(('run_id','tenant_id','user_id','conversation_id'),1)}
    state.update(memory_context='旧版本已删除偏好不得复用',latest_user_message='你好',history=[{'role':'user','content':'你好'}])
    await graph._generate_reply(state)
    assert scopes[0].service is memory and scopes[0].purpose=='conversation_reply'
    assert current_memory_request() is None
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


def test_mail_agent_no_longer_accepts_unverified_memory_strings_or_callbacks():
    import inspect
    from aegis_agent_worker.agent.specialists.mail_agent import MailAgent
    params=inspect.signature(MailAgent.generate_reply_draft).parameters
    assert 'memory_context' not in params and 'memory_context_loader' not in params
