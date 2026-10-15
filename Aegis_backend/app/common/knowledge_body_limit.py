"""仅限制知识上传请求体；不依赖可伪造的Content-Length或无限multipart暂存。"""
from starlette.responses import JSONResponse


class KnowledgeBodyLimit:
    def __init__(self,app,maximum=None):
        self.app,self.maximum=app,maximum

    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('method')!='POST' or scope.get('path') not in {
            '/knowledge/documents','/knowledge/documents/upload'}:
            return await self.app(scope,receive,send)
        # 有界预读后才交给multipart/JSON解析，超限直接413，不写入源文件。
        messages,total=[],0
        maximum=self.maximum or scope['app'].state.settings.knowledge_max_source_bytes+65536
        while True:
            message=await receive()
            if message['type']=='http.disconnect': return
            total+=len(message.get('body',b''))
            if total>maximum:
                await JSONResponse(status_code=413,content={'detail':{'code':'KNOWLEDGE_SOURCE_TOO_LARGE',
                    'message':'导入请求体超过上限'}})(scope,receive,send)
                return
            messages.append(message)
            if not message.get('more_body',False): break
        iterator=iter(messages)
        async def replay():
            try: return next(iterator)
            except StopIteration: return await receive()
        await self.app(scope,replay,send)
