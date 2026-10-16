"""Qdrant只读查询适配；不创建集合，不使用写密钥，不读取payload正文。"""
import math
from uuid import UUID
import httpx
from aegis_agent_worker.vector.contracts import VectorIndexError
from aegis_agent_worker.retrieval.ranking import SearchHit


class QdrantSearch:
    def __init__(self, settings, client=None):
        self._settings, self._client = settings, client

    async def search(self, context, versions, vector, profile, limit):
        s = self._settings
        if not s.qdrant_enabled or not s.qdrant_url or not s.qdrant_api_key:
            raise VectorIndexError('QDRANT_NOT_CONFIGURED', '向量查询需要Qdrant读取配置')
        if (not versions or len(versions)>1000 or not 1 <= limit <= 50
            or any(not isinstance(value, UUID) for value in versions)
            or not isinstance(context.tenant_id, UUID) or not isinstance(context.user_id, UUID)):
            raise VectorIndexError('QDRANT_FILTER_INVALID', '禁止无身份或无范围向量查询')
        if len(vector)!=profile.dimensions or any(type(value) not in (float,int) or not math.isfinite(value) for value in vector):
            raise VectorIndexError('QDRANT_QUERY_INVALID', '查询向量维度或数值非法')
        must = [{'key':'tenant_id','match':{'value':str(context.tenant_id)}},
                {'key':'version_id','match':{'any':[str(value) for value in versions]}}]
        for key, value in (('model_provider',profile.provider),('model',profile.model),
                           ('model_version',profile.version),('dimensions',profile.dimensions)):
            must.append({'key':key,'match':{'value':value}})
        # ACL事实来自SQL版本白名单，不依赖可能已过期的payload.visibility。
        payload = {'query':vector,'filter':{'must':must},'limit':limit,
                   'with_payload':False,'with_vector':False,'params':{'exact':True}}
        own = self._client is None
        http = self._client or httpx.AsyncClient()
        try:
            response = await http.post(str(s.qdrant_url).rstrip('/')+'/collections/'+s.qdrant_collection+'/points/query',
                json=payload,headers={'api-key':s.qdrant_api_key.get_secret_value()},timeout=s.qdrant_request_timeout_seconds)
            if response.status_code in (401,403):
                raise VectorIndexError('QDRANT_AUTH_FAILED','向量查询未获授权')
            if response.status_code in (400,404,422):
                raise VectorIndexError('QDRANT_QUERY_REJECTED','向量集合或过滤配置不匹配，查询被拒绝')
            response.raise_for_status()
            body = response.json()
            if body.get('status')!='ok':
                raise ValueError()
            points = body['result']['points']
            if not isinstance(points,list) or len(points)>limit:
                raise ValueError()
            hits=[]
            for item in points:
                score=item['score']
                if type(score) not in (float,int) or not math.isfinite(score):
                    raise ValueError()
                hits.append(SearchHit(UUID(item['id']),float(score)))
            return hits
        except httpx.TimeoutException:
            raise VectorIndexError('QDRANT_TIMEOUT','向量查询超时') from None
        except (httpx.HTTPError,ValueError,KeyError,TypeError,AttributeError):
            raise VectorIndexError('QDRANT_UNAVAILABLE','向量查询失败或返回无效') from None
        finally:
            if own:
                await http.aclose()
