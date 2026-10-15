"""Qdrant REST适配；有界请求、显式写密钥，拒绝维度不匹配/未经确认的写入。"""
from hashlib import sha256
import httpx
from aegis_agent_worker.vector.contracts import VectorIndexError


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
