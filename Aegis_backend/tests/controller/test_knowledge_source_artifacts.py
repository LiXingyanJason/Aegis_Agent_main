"""历史来源卡片即时重验；不读取Worker实现、不使用云服务。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import pytest
from app.service.knowledge_artifact_service import KnowledgeArtifactService
from app.service.conversation_service import ConversationService


def source(number=1):
    return dict(source_no=number,document_id=str(UUID(int=100)),version_id=str(UUID(int=101)),chunk_id=str(UUID(int=101+number)),
        title='虚构政策',version_label='v1',section='正文',start_offset=0,end_offset=4,excerpt='虚构资料',content_hash='b'*64)


def row(number=1):
    data=source(number)
    data.pop('source_no')
    return {key:UUID(value) if key.endswith('_id') else value for key,value in data.items()}


@pytest.mark.asyncio
async def test_current_source_revalidated_and_old_input_not_mutated():
    repository=SimpleNamespace(load_visible=AsyncMock(return_value=[row()]))
    artifact={'artifact_type':'knowledge_sources','payload':{'sources':[source()]}}
    result=await KnowledgeArtifactService(repository).refresh(None,SimpleNamespace(),artifact)
    assert result['payload']=={'sources':[source()],'availability':'available','has_conflict':False}
    assert 'availability' not in artifact['payload']
    assert repository.load_visible.await_args.args[-1]==[UUID(int=102)]


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[None,{'title':'新标题'},{'version_id':UUID(int=999)},
    {'content_hash':'c'*64},{'excerpt':'新正文'},{'start_offset':1}])
async def test_revoked_changed_or_replaced_source_never_returns_old_excerpt(change):
    rows=[] if change is None else [{**row(),**change}]
    repository=SimpleNamespace(load_visible=AsyncMock(return_value=rows))
    result=await KnowledgeArtifactService(repository).refresh(None,SimpleNamespace(),{'payload':{'sources':[source()]}})
    assert result['payload']['sources']==[] and result['payload']['availability']=='unavailable'
    assert '虚构资料' not in str(result) and '新标题' not in str(result)


@pytest.mark.asyncio
async def test_partial_source_keeps_historical_numbers_no_new_reference():
    repository=SimpleNamespace(load_visible=AsyncMock(return_value=[row(2)]))
    result=await KnowledgeArtifactService(repository).refresh(None,SimpleNamespace(),{'payload':{'sources':[source(),source(2)],'has_conflict':True}})
    assert result['payload']['availability']=='partial'
    assert result['payload']['sources'][0]['source_no']==2


@pytest.mark.asyncio
@pytest.mark.parametrize('payload',[None,{'sources':[]},{'sources':[{'excerpt':'不安全快照'}]},
    {'sources':[source(),source()]},{'sources':[source()],'tenant_id':'fake'}])
async def test_invalid_payload_fails_closed_without_query(payload):
    repository=SimpleNamespace(load_visible=AsyncMock())
    result=await KnowledgeArtifactService(repository).refresh(None,SimpleNamespace(),{'payload':payload})
    assert result['payload']['sources']==[] and result['payload']['availability']=='unavailable'
    repository.load_visible.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversation_permission_precedes_source_read_and_other_cards_unchanged():
    repository=SimpleNamespace(find_detail_for_user=AsyncMock(return_value=None),
        list_active_artifacts_for_conversation=AsyncMock())
    guard=SimpleNamespace(refresh=AsyncMock())
    user=SimpleNamespace(id=UUID(int=10),tenant_id=UUID(int=1))
    service=ConversationService(repository,guard)
    assert await service.list_active_artifacts(None,user,UUID(int=4)) is None
    guard.refresh.assert_not_awaited()
    repository.find_detail_for_user.return_value={'id':UUID(int=4)}
    mail={'artifact_type':'mail_messages','payload':{'existing':'unchanged'}}
    knowledge={'artifact_type':'knowledge_sources','payload':{'sources':[source()]}}
    repository.list_active_artifacts_for_conversation.return_value=[mail,knowledge]
    guard.refresh.return_value={'artifact_type':'knowledge_sources','payload':{'sources':[],'availability':'unavailable'}}
    result=await service.list_active_artifacts(None,user,UUID(int=4))
    assert result[0]==mail and result[1]['payload']['sources']==[]
    guard.refresh.assert_awaited_once()
