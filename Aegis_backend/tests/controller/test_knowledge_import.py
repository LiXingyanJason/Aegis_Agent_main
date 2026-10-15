"""导入安全/幂等/提交后投递离线验证。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import pytest
from app.param.knowledge_param import KnowledgeImportMetadata
from app.service.knowledge_import_service import KnowledgeImportService,KnowledgeImportError
from app.service.knowledge_dispatch_service import KnowledgeDispatchService
from app.storage.local_document_storage import LocalDocumentStorage
from app.config.database import tenant_transaction,TenantContext

USER=SimpleNamespace(id=UUID(int=10),tenant_id=UUID(int=1))


def metadata(**values):
    return KnowledgeImportMetadata(title='测试',category='project',client_request_id='test',external_embedding_allowed=True,**values)


def imports(tmp_path):
    repo=SimpleNamespace(lock_request=AsyncMock(),replay=AsyncMock(return_value=None),create=AsyncMock(return_value={'status':'uploaded'}))
    settings=SimpleNamespace(knowledge_max_source_bytes=1024,embedding_provider='openai_compatible',
        embedding_model='test',embedding_model_version='v1',embedding_dimensions=3)
    return KnowledgeImportService(settings,LocalDocumentStorage(tmp_path,1024),repo),repo


@pytest.mark.asyncio
@pytest.mark.parametrize('content,filename,code',[(b'a','../bad.txt','KNOWLEDGE_SOURCE_TYPE_INVALID'),
    (b'\xff','bad.txt','KNOWLEDGE_SOURCE_ENCODING_INVALID'),(b'a'*1025,'big.txt','KNOWLEDGE_SOURCE_TOO_LARGE'),
    (b'a','bad.pdf','KNOWLEDGE_SOURCE_TYPE_INVALID')])
async def test_invalid_files_never_reach_sql_or_storage(tmp_path,content,filename,code):
    service,repo=imports(tmp_path)
    with pytest.raises(KnowledgeImportError) as error:
        await service.import_source(None,USER,metadata(),content,filename)
    assert error.value.code==code
    repo.create.assert_not_awaited()
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_upload_does_not_require_outbound_approval_or_embedding_configuration(tmp_path):
    service,repo=imports(tmp_path)
    service._settings.embedding_provider=None
    service._settings.embedding_model=None
    service._settings.embedding_model_version=None
    await service.import_source(None,USER,metadata(is_sensitive=True),b'a','a.txt')
    denied=metadata().model_copy(update={'external_embedding_allowed':False})
    await service.import_source(None,USER,denied,b'a','a.txt')
    assert repo.create.await_count==2
    assert 'profile' not in repo.create.await_args.kwargs


@pytest.mark.asyncio
async def test_same_request_replays_and_changed_payload_conflicts(tmp_path):
    service,repo=imports(tmp_path)
    await service.import_source(None,USER,metadata(),b'first','a.txt')
    digest=repo.create.await_args.kwargs['request_hash']
    repo.replay.return_value=dict(request_hash=digest,document_id=UUID(int=1),version_id=UUID(int=2),job_id=None,
        status='uploaded',index_status='not_indexed',enabled=False)
    assert (await service.import_source(None,USER,metadata(),b'first','a.txt'))['idempotent_replay']
    with pytest.raises(KnowledgeImportError) as error:
        await service.import_source(None,USER,metadata(),b'changed','a.txt')
    assert error.value.code=='KNOWLEDGE_IDEMPOTENCY_CONFLICT'
    assert repo.create.await_count==1 and len(list(tmp_path.iterdir()))==1


@pytest.mark.asyncio
async def test_sql_failure_cleans_only_new_source(tmp_path):
    service,repo=imports(tmp_path)
    repo.create.side_effect=ValueError('failure')
    with pytest.raises(ValueError): await service.import_source(None,USER,metadata(),b'a','a.txt')
    assert not list(tmp_path.iterdir())


def test_metadata_does_not_accept_paths_or_identity():
    with pytest.raises(ValueError): KnowledgeImportMetadata(title='test',category='project',client_request_id='x',tenant_id='other')


@pytest.mark.asyncio
async def test_redis_is_called_only_after_commit_and_rollback_never_dispatches():
    from contextlib import asynccontextmanager
    class Session:
        committed=False
        @asynccontextmanager
        async def begin(self):
            yield
            self.committed=True
        async def execute(self,*args): pass
    redis=SimpleNamespace(rpush=AsyncMock())
    dispatcher=KnowledgeDispatchService(redis,'index')
    session=Session()
    async with tenant_transaction(session,TenantContext(USER.tenant_id,USER.id)):
        dispatcher.enqueue_after_commit({'job_id':UUID(int=1)},USER)
        redis.rpush.assert_not_awaited()
    assert session.committed and redis.rpush.await_count==1
    with pytest.raises(ValueError):
        async with tenant_transaction(Session(),TenantContext(USER.tenant_id,USER.id)):
            dispatcher.enqueue_after_commit({'job_id':UUID(int=2)},USER)
            raise ValueError('rollback')
    assert redis.rpush.await_count==1
