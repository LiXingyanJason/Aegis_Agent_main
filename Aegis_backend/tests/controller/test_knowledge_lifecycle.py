"""上传/启动索引/启用的独立业务守卫。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import pytest
from app.service.knowledge_import_service import KnowledgeImportService,KnowledgeImportError
from app.param.knowledge_param import KnowledgeIndexStartParam,KnowledgeActivationParam

USER=SimpleNamespace(id=UUID(int=1),tenant_id=UUID(int=2))
DOC=UUID(int=3)


def setup(**changes):
    source=dict(document_id=DOC,version_id=UUID(int=4),job_id=None,job_status=None,enabled=False,
        is_sensitive=False,external_embedding_allowed=False,current_version_id=None,version_status='pending',
        embedding_provider=None,embedding_model=None,embedding_model_version=None,embedding_dimensions=None,
        total_chunks=None,processed_chunks=None)
    source.update(changes)
    repo=SimpleNamespace(owned_source=AsyncMock(return_value=source),create_index_job=AsyncMock(),activation=AsyncMock())
    config=SimpleNamespace(embedding_provider='openai_compatible',embedding_model='test',embedding_model_version='v1',embedding_dimensions=3)
    return KnowledgeImportService(config,None,repo),repo


@pytest.mark.asyncio
@pytest.mark.parametrize('changes,param,code',[
    ({},KnowledgeIndexStartParam(),'KNOWLEDGE_DATA_POLICY_BLOCKED'),
    ({'is_sensitive':True},KnowledgeIndexStartParam(external_embedding_allowed=True),'KNOWLEDGE_DATA_POLICY_BLOCKED'),
    ({'version_status':'retired'},KnowledgeIndexStartParam(external_embedding_allowed=True),'KNOWLEDGE_SOURCE_UNAVAILABLE')])
async def test_index_requires_explicit_consent_nonsensitive_valid_source(changes,param,code):
    service,repo=setup(**changes)
    with pytest.raises(KnowledgeImportError) as error:
        await service.start_index(None,USER,DOC,param)
    assert error.value.code==code
    repo.create_index_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_reuses_job_without_creating_or_resetting_budget():
    service,repo=setup(job_id=UUID(int=5),job_status='running',version_status='indexing')
    item=await service.start_index(None,USER,DOC,KnowledgeIndexStartParam(external_embedding_allowed=True))
    assert item['idempotent_replay'] and item['status']=='running'
    repo.create_index_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('status',['failed','cancelled'])
async def test_start_does_not_replace_failed_job(status):
    service,repo=setup(job_id=UUID(int=5),job_status=status)
    with pytest.raises(KnowledgeImportError):
        await service.start_index(None,USER,DOC,KnowledgeIndexStartParam(external_embedding_allowed=True))
    repo.create_index_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_requires_configuration_only_at_index_time():
    service,repo=setup()
    service._settings.embedding_model=None
    with pytest.raises(KnowledgeImportError) as error:
        await service.start_index(None,USER,DOC,KnowledgeIndexStartParam(external_embedding_allowed=True))
    assert error.value.code=='KNOWLEDGE_EMBEDDING_NOT_CONFIGURED'


@pytest.mark.asyncio
@pytest.mark.parametrize('changes',[{}, {'job_id':UUID(int=5),'job_status':'running','version_status':'indexing'},
    {'job_id':UUID(int=5),'job_status':'succeeded','version_status':'ready','current_version_id':UUID(int=4),
     'total_chunks':2,'processed_chunks':1,'external_embedding_allowed':True}])
async def test_activation_requires_all_confirmed_chunks(changes):
    service,repo=setup(**changes)
    with pytest.raises(KnowledgeImportError) as error:
        await service.activate(None,USER,DOC,True)
    assert error.value.code=='KNOWLEDGE_DOCUMENT_NOT_READY'
    repo.activation.assert_not_awaited()


@pytest.mark.asyncio
async def test_activate_and_disable_do_not_invoke_index_or_reset_budget():
    service,repo=setup(job_id=UUID(int=5),job_status='succeeded',version_status='ready',current_version_id=UUID(int=4),
                       total_chunks=2,processed_chunks=2,external_embedding_allowed=True)
    assert (await service.activate(None,USER,DOC,True))['enabled']
    assert not (await service.activate(None,USER,DOC,False))['enabled']
    assert repo.activation.await_count==2
    repo.create_index_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_reader_or_cross_tenant_cannot_manage_document():
    service,repo=setup()
    repo.owned_source.return_value=None
    with pytest.raises(KnowledgeImportError) as error:
        await service.activate(None,USER,DOC,False)
    assert error.value.status==404
    repo.activation.assert_not_awaited()


def test_activation_rejects_model_supplied_extra_fields_or_nonboolean():
    with pytest.raises(ValueError): KnowledgeActivationParam(enabled=True,tenant_id='other')
    with pytest.raises(ValueError): KnowledgeActivationParam(enabled='true')
