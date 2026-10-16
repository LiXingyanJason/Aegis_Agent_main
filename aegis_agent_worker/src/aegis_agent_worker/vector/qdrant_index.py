"""Qdrant REST适配；有界请求、显式写密钥，拒绝维度不匹配/未经确认的写入。"""
from hashlib import sha256
import httpx
from aegis_agent_worker.vector.contracts import VectorIndexError


# 与查询过滤及按文档清理使用的payload字段一致；不包含正文。
PAYLOAD_INDEXES = {
    'tenant_id': 'keyword', 'version_id': 'keyword', 'document_id': 'keyword',
    'model_provider': 'keyword', 'model': 'keyword', 'model_version': 'keyword',
    'dimensions': 'integer',
}


def _missing_payload_indexes(current):
    schema = current.get('payload_schema', {})
    if not isinstance(schema, dict):
        raise VectorIndexError('QDRANT_INDEX_SCHEMA_INVALID', '集合过滤索引配置无效')
    missing = []
    for field, expected in PAYLOAD_INDEXES.items():
        if field not in schema:
            missing.append(field)
            continue
        entry = schema[field]
        actual = entry.get('data_type') if isinstance(entry, dict) else None
        # 已有UUID索引同样支持字符串UUID精确过滤，不删除重建。
        compatible = {expected, 'uuid'} if field in {'tenant_id', 'version_id', 'document_id'} else {expected}
        if actual not in compatible:
            raise VectorIndexError('QDRANT_INDEX_SCHEMA_MISMATCH', '集合过滤索引类型不匹配，不自动删除重建')
    return missing


class QdrantVectorIndex:
    def __init__(self,settings,client=None):
        self._settings,self._client=settings,client
        self.namespace=sha256((str(settings.qdrant_url).rstrip('/')+'/'+settings.qdrant_collection).encode()).hexdigest()

    async def _request(self,method,suffix,*,payload=None,allow_missing=False):
        s=self._settings
        if not s.qdrant_enabled or not s.qdrant_url or not s.qdrant_write_api_key:
            raise VectorIndexError('QDRANT_NOT_CONFIGURED','索引需要启用Qdrant并配置写密钥')
        own=self._client is None
        http=self._client or httpx.AsyncClient()
        try:
            response=await http.request(method,str(s.qdrant_url).rstrip('/')+'/collections/'+s.qdrant_collection+suffix,
                json=payload,headers={'api-key':s.qdrant_write_api_key.get_secret_value()},timeout=s.qdrant_request_timeout_seconds)
            if response.status_code==404 and allow_missing:
                return None
            response.raise_for_status()
            data=response.json()
            if data.get('status')!='ok':
                raise ValueError()
            return data['result']
        except (httpx.HTTPError,ValueError,KeyError,TypeError):
            raise VectorIndexError('QDRANT_UNAVAILABLE','向量索引服务不可用或返回无效') from None
        finally:
            if own: await http.aclose()

    async def ensure_collection(self,profile):
        current=await self._request('GET','',allow_missing=True)
        if current is None:
            try:
                await self._request('PUT','',payload={'vectors':{'size':profile.dimensions,'distance':'Cosine'}})
            except VectorIndexError:
                # 并发创建时复查，只接受与本任务相同的配置。
                current=await self._request('GET','')
            else:
                current=await self._request('GET','')
        vectors=current['config']['params']['vectors']
        if vectors.get('size')!=profile.dimensions or vectors.get('distance')!='Cosine':
            raise VectorIndexError('QDRANT_PROFILE_MISMATCH','集合维度/距离与模型空间不一致，不自动删除重建')
        return await self.ensure_payload_indexes(current)

    async def ensure_payload_indexes(self, current=None):
        """索引端幂等补建过滤索引，也可修复已有集合；查询端不持有写权限。"""
        if current is None:
            current = await self._request('GET', '')
        missing = _missing_payload_indexes(current)
        for field in missing:
            result = await self._request('PUT', '/index?wait=true', payload={
                'field_name': field, 'field_schema': PAYLOAD_INDEXES[field],
            })
            if not isinstance(result, dict) or result.get('status') != 'completed':
                raise VectorIndexError('QDRANT_WRITE_UNCONFIRMED', '过滤索引创建尚未确认完成')
        if missing:
            current = await self._request('GET', '')
            if _missing_payload_indexes(current):
                raise VectorIndexError('QDRANT_INDEX_UNCONFIRMED', '集合仍缺少必要过滤索引')
        return current

    async def upsert(self,points):
        if not points or len(points)>16:
            raise VectorIndexError('QDRANT_BATCH_INVALID','索引批次为空或超限')
        result=await self._request('PUT','/points?wait=true',payload={'points':points})
        if not isinstance(result,dict) or result.get('status')!='completed':
            raise VectorIndexError('QDRANT_WRITE_UNCONFIRMED','向量写入尚未确认完成')

    async def delete_document(self,tenant_id,document_id):
        result=await self._request('POST','/points/delete?wait=true',payload={'filter':{'must':[
            {'key':'tenant_id','match':{'value':str(tenant_id)}},
            {'key':'document_id','match':{'value':str(document_id)}}]}})
        if result.get('status')!='completed':
            raise VectorIndexError('QDRANT_WRITE_UNCONFIRMED','清理尚未确认')
