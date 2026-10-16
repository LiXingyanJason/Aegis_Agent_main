"""过滤索引初始化/修复离线回归，不创建真实云端数据。"""
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from aegis_agent_worker.embedding.contracts import EmbeddingProfile
from aegis_agent_worker.vector.contracts import VectorIndexError
from aegis_agent_worker.vector.qdrant_index import PAYLOAD_INDEXES, QdrantVectorIndex


PROFILE = EmbeddingProfile('openai_compatible', 'test', 'v1', 3)


def settings():
    return SimpleNamespace(qdrant_enabled=True, qdrant_url='https://test.invalid',
        qdrant_collection='kb', qdrant_write_api_key=SecretStr('write-only'),
        qdrant_request_timeout_seconds=2)


@pytest.mark.asyncio
@pytest.mark.parametrize('existing', [False, True])
async def test_new_and_existing_collection_create_all_missing_indexes_and_repeat_noop(existing):
    created = existing
    schema, calls = {}, []

    def respond(request):
        nonlocal created
        calls.append((request.method, request.url.path))
        assert request.headers['api-key'] == 'write-only'
        if request.method == 'GET':
            if not created:
                return httpx.Response(404)
            return httpx.Response(200, json={'status':'ok', 'result':{
                'config':{'params':{'vectors':{'size':3, 'distance':'Cosine'}}},
                'points_count':2 if existing else 0, 'payload_schema':dict(schema)}})
        data = json.loads(request.content)
        if request.url.path.endswith('/index'):
            assert request.url.query == b'wait=true'
            assert data['field_schema'] == PAYLOAD_INDEXES[data['field_name']]
            schema[data['field_name']] = {'data_type':data['field_schema']}
            return httpx.Response(200, json={'status':'ok', 'result':{'status':'completed'}})
        assert request.method == 'PUT' and request.url.path == '/collections/kb'
        created = True
        return httpx.Response(200, json={'status':'ok', 'result':True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        index = QdrantVectorIndex(settings(), http)
        result = await index.ensure_collection(PROFILE)
        assert set(schema) == set(PAYLOAD_INDEXES)
        assert result['points_count'] == (2 if existing else 0)
        before = len(calls)
        await index.ensure_collection(PROFILE)
        assert calls[before:] == [('GET', '/collections/kb')]
    assert not any(method in {'DELETE', 'POST'} or path.endswith('/points') for method,path in calls)


@pytest.mark.asyncio
async def test_existing_uuid_indexes_are_preserved():
    schema = {key:{'data_type':('uuid' if key.endswith('_id') else value)} for key,value in PAYLOAD_INDEXES.items()}
    def respond(request):
        assert request.method == 'GET'
        return httpx.Response(200, json={'status':'ok', 'result':{'payload_schema':schema}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await QdrantVectorIndex(settings(), http).ensure_payload_indexes()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,code', [
    ('wrong_type', 'QDRANT_INDEX_SCHEMA_MISMATCH'),
    ('unconfirmed', 'QDRANT_WRITE_UNCONFIRMED'),
    ('not_visible', 'QDRANT_INDEX_UNCONFIRMED'),
    ('unavailable', 'QDRANT_UNAVAILABLE'),
])
async def test_invalid_or_unconfirmed_indexes_fail_closed(mode, code):
    writes = []
    def respond(request):
        if request.method == 'GET':
            schema = {'dimensions':{'data_type':'keyword'}} if mode == 'wrong_type' else {}
            return httpx.Response(200, json={'status':'ok', 'result':{'payload_schema':schema}})
        writes.append(request)
        if mode == 'unavailable':
            return httpx.Response(503)
        return httpx.Response(200, json={'status':'ok', 'result':{
            'status':'acknowledged' if mode == 'unconfirmed' else 'completed'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(VectorIndexError) as error:
            await QdrantVectorIndex(settings(), http).ensure_payload_indexes()
    assert error.value.code == code
    if mode == 'wrong_type':
        assert writes == []
