"""只读REST协议、强制过滤与错误分类，不访问真实云端。"""
import json
from types import SimpleNamespace
from uuid import UUID
import httpx
import pytest
from pydantic import SecretStr
from aegis_agent_worker.vector.qdrant_search import QdrantSearch
from aegis_agent_worker.vector.contracts import VectorIndexError
from aegis_agent_worker.embedding.contracts import EmbeddingProfile
from aegis_agent_worker.tool.contracts import ToolContext


def settings():
    return SimpleNamespace(qdrant_enabled=True,qdrant_url='https://test.example',qdrant_collection='kb',
        qdrant_api_key=SecretStr('read-only'),qdrant_write_api_key=SecretStr('never-use'),qdrant_request_timeout_seconds=5)


CONTEXT=ToolContext(UUID(int=1),UUID(int=10),UUID(int=600))
PROFILE=EmbeddingProfile('openai_compatible','test','v1',3)


@pytest.mark.asyncio
async def test_post_filtered_query_read_key_no_body_or_vectors_requested():
    requests=[]
    def respond(request):
        requests.append(request)
        payload=json.loads(request.content)
        assert payload['with_payload'] is False and payload['with_vector'] is False
        assert payload['filter']['must'][0]=={'key':'tenant_id','match':{'value':str(CONTEXT.tenant_id)}}
        assert payload['filter']['must'][1]=={'key':'version_id','match':{'any':[str(UUID(int=101))]}}
        assert {item['key'] for item in payload['filter']['must']}=={'tenant_id','version_id','model_provider','model','model_version','dimensions'}
        assert request.headers['api-key']=='read-only'
        assert request.method=='POST' and request.url.path=='/collections/kb/points/query'
        return httpx.Response(200,json={'status':'ok','result':{'points':[{'id':str(UUID(int=201)),'score':0.9}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result=await QdrantSearch(settings(),http).search(CONTEXT,[UUID(int=101)],[1.,0.,0.],PROFILE,20)
    assert len(requests)==1 and result[0].chunk_id.int==201


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code',[(500,'QDRANT_UNAVAILABLE'),(403,'QDRANT_AUTH_FAILED'),(404,'QDRANT_QUERY_REJECTED'),(400,'QDRANT_QUERY_REJECTED')])
async def test_failure_classification(status,code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(status))) as http:
        with pytest.raises(VectorIndexError) as error:
            await QdrantSearch(settings(),http).search(CONTEXT,[UUID(int=101)],[1.,0.,0.],PROFILE,20)
    assert error.value.code==code


@pytest.mark.asyncio
@pytest.mark.parametrize('versions,vector', [([], [1.,0.,0.]),([UUID(int=101)],[1.]),([UUID(int=101)],[float('nan'),0.,0.])])
async def test_invalid_filter_or_vector_no_request(versions,vector):
    def forbidden(request): raise AssertionError('不应执行网络请求')
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        with pytest.raises(VectorIndexError):
            await QdrantSearch(settings(),http).search(CONTEXT,versions,vector,PROFILE,20)


@pytest.mark.asyncio
async def test_timeout_and_invalid_response_stable():
    def timeout(request): raise httpx.ReadTimeout('timeout',request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        with pytest.raises(VectorIndexError) as error:
            await QdrantSearch(settings(),http).search(CONTEXT,[UUID(int=101)],[1.,0.,0.],PROFILE,20)
        assert error.value.code=='QDRANT_TIMEOUT'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'status':'ok','result':{'points':[{'id':'bad','score':0.2}]}}))) as http:
        with pytest.raises(VectorIndexError) as error:
            await QdrantSearch(settings(),http).search(CONTEXT,[UUID(int=101)],[1.,0.,0.],PROFILE,20)
        assert error.value.code=='QDRANT_UNAVAILABLE'
