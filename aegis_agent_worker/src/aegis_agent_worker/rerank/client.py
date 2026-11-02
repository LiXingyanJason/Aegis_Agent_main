"""DashScope text-rerank HTTP protocol, bounded streamed response, no redirects."""
from contextvars import ContextVar
import json
from math import isfinite
import httpx
from aegis_agent_worker.common.usage import TokenUsage


class RerankError(Exception):
    def __init__(self,code,message): self.code=code;super().__init__(message)


def validate_scores(rows,count):
    if not isinstance(rows,list) or len(rows)!=count:
        raise RerankError('RERANK_RESPONSE_INVALID','重排返回数量不完整')
    scores={}
    for row in rows:
        if not isinstance(row,dict): raise RerankError('RERANK_RESPONSE_INVALID','重排结果无效')
        i,s=row.get('index'),row.get('relevance_score')
        if (type(i) is not int or not 0<=i<count or i in scores or type(s) not in (int,float)
                or not isfinite(s) or not 0<=s<=1):
            raise RerankError('RERANK_RESPONSE_INVALID','重排索引或分数无效')
        scores[i]=float(s)
    return sorted(scores.items(),key=lambda pair:(-pair[1],pair[0]))


class RerankClient:
    def __init__(self,settings,client=None):
        self.settings,self.client=settings,client
        self._usage=ContextVar('rerank_usage',default=None)

    @property
    def usage(self): return self._usage.get() or TokenUsage()

    async def rank(self,query,documents,policy):
        self._usage.set(None)
        s=self.settings
        if not s.rerank_enabled or not s.rerank_api_url or not s.rerank_api_key:
            raise RerankError('RERANK_NOT_CONFIGURED','重排服务尚未配置')
        owned=self.client is None
        client=self.client or httpx.AsyncClient(follow_redirects=False)
        parameters={'top_n':len(documents)}
        # The 3.7 API documents top_n/instruct; omit the legacy optional
        # return_documents flag. Only indices/scores are consumed in either case.
        if s.rerank_model=='qwen3-rerank':parameters['return_documents']=False
        try:
            async with client.stream('POST',str(s.rerank_api_url),
                headers={'Authorization':'Bearer '+s.rerank_api_key.get_secret_value()},
                json={'model':s.rerank_model,'input':{'query':query,'documents':documents},
                      'parameters':parameters},
                timeout=policy.timeout_seconds,follow_redirects=False) as response:
                if response.status_code==429: raise RerankError('RERANK_RATE_LIMITED','重排服务限流')
                if response.status_code!=200: raise RerankError('RERANK_UNAVAILABLE','重排服务调用失败')
                body=bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body)>policy.max_response_bytes:
                        raise RerankError('RERANK_RESPONSE_INVALID','重排响应超限')
            try:
                payload=json.loads(body)
                if not isinstance(payload,dict): raise ValueError()
                self._usage.set(TokenUsage.from_payload(payload))
                return validate_scores(payload['output']['results'],len(documents))
            except (ValueError,KeyError,TypeError):
                raise RerankError('RERANK_RESPONSE_INVALID','重排响应格式无效') from None
        except httpx.TimeoutException:
            raise RerankError('RERANK_TIMEOUT','重排请求超时') from None
        except httpx.HTTPError:
            raise RerankError('RERANK_UNAVAILABLE','重排网络不可用') from None
        finally:
            if owned: await client.aclose()
