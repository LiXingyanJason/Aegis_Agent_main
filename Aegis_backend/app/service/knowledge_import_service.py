"""受控源文件导入，不调用LLM/Embedding，不实现已有文档更新。"""
from hashlib import sha256
import json
from uuid import uuid4
from app.repository.knowledge_import_repository import KnowledgeImportRepository


class KnowledgeImportError(ValueError):
    def __init__(self, code, message, status=422):
        self.code, self.status = code, status
        super().__init__(message)


class KnowledgeImportService:
    def __init__(self, settings, storage, repository=None):
        self._settings, self._storage = settings, storage
        self._repository = repository or KnowledgeImportRepository()

    @property
    def max_source_bytes(self):
        return self._settings.knowledge_max_source_bytes

    async def import_source(self, session, user, metadata, content, filename):
        # 上传只保存，不产生外发或计费；索引入口另行核验授权和敏感标记。
        if not content or len(content)>self._settings.knowledge_max_source_bytes:
            raise KnowledgeImportError("KNOWLEDGE_SOURCE_TOO_LARGE", "源文件为空或超过大小上限", 413)
        if (not filename or len(filename)>255 or any(char in filename for char in ('/', '\\', '\0'))
            or not filename.lower().endswith(('.md','.txt'))):
            raise KnowledgeImportError("KNOWLEDGE_SOURCE_TYPE_INVALID", "仅支持文件名不含路径的UTF-8 Markdown/TXT")
        try:
            decoded = content.decode('utf-8-sig')
            if not decoded.strip() or '\0' in decoded:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise KnowledgeImportError("KNOWLEDGE_SOURCE_ENCODING_INVALID", "仅支持非空UTF-8文本") from None
        source_hash = sha256(content).hexdigest()
        request_hash = sha256(json.dumps({"metadata":metadata.model_dump(mode='json'), "filename":filename,
            "source_hash":source_hash},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        await self._repository.lock_request(session,user,metadata.client_request_id)
        replay = await self._repository.replay(session,user,metadata.client_request_id)
        if replay:
            if replay['request_hash'] != request_hash:
                raise KnowledgeImportError("KNOWLEDGE_IDEMPOTENCY_CONFLICT", "同一请求标识已用于不同资料",409)
            return {key:replay[key] for key in ('document_id','version_id','job_id','status','index_status','enabled')} | {"idempotent_replay":True}
        key = str(uuid4())
        stored = False
        try:
            await self._storage.put(key,content)
            stored = True
            return await self._repository.create(session,user,metadata,storage_key=key,filename=filename,
                mime='text/markdown' if filename.lower().endswith('.md') else 'text/plain', source_bytes=len(content),
                source_hash=source_hash,request_hash=request_hash)
        except BaseException:
            # 登记失败的本次独立对象可删除；最终事务提交失败的孤立对象由显式清理工具处理。
            if stored:
                await self._storage.delete(key)
            raise

    async def list_documents(self, session,user,limit):
        return await self._repository.list_documents(session,user,limit)

    @staticmethod
    def index_status(source):
        if source['job_id'] is None:
            return 'not_indexed'
        if source['job_status']=='succeeded':
            return 'ready' if source['version_status']=='ready' else 'unavailable'
        return source['job_status']

    async def start_index(self,session,user,document_id,param):
        source=await self._repository.owned_source(session,user,document_id)
        if source is None:
            raise KnowledgeImportError('KNOWLEDGE_DOCUMENT_NOT_FOUND','资料不存在或无权操作',404)
        if source['is_sensitive'] or not param.external_embedding_allowed:
            raise KnowledgeImportError('KNOWLEDGE_DATA_POLICY_BLOCKED','建立索引必须明确授权外发，首期敏感资料不可索引')
        if source['version_status']=='retired':
            raise KnowledgeImportError('KNOWLEDGE_SOURCE_UNAVAILABLE','资料已失效',409)
        if source['job_id'] is not None:
            if source['job_status'] not in ('queued','running','succeeded'):
                raise KnowledgeImportError('KNOWLEDGE_JOB_NOT_RETRYABLE','已有失败/取消任务，请使用失败任务重试入口',409)
            return dict(document_id=document_id,version_id=source['version_id'],job_id=source['job_id'],
                status=source['job_status'],index_status=self.index_status(source),enabled=source['enabled'],idempotent_replay=True)
        if source['version_status'] not in ('pending','indexing','failed'):
            raise KnowledgeImportError('KNOWLEDGE_INDEX_STATE_INVALID','内容状态与索引记录不一致，不能自动重新索引',409)
        s=self._settings
        if not all((s.embedding_provider,s.embedding_model,s.embedding_model_version)):
            raise KnowledgeImportError('KNOWLEDGE_EMBEDDING_NOT_CONFIGURED','启动索引前需配置与Worker一致的模型元数据',503)
        profile=dict(provider=s.embedding_provider,model=s.embedding_model,model_version=s.embedding_model_version,dimensions=s.embedding_dimensions)
        prior=(source['embedding_provider'],source['embedding_model'],source['embedding_model_version'],source['embedding_dimensions'])
        if any(value is not None for value in prior) and prior!=(profile['provider'],profile['model'],profile['model_version'],profile['dimensions']):
            raise KnowledgeImportError('EMBEDDING_PROFILE_MISMATCH','旧内容记录已绑定其他模型空间',409)
        return await self._repository.create_index_job(session,user,source,profile)

    async def activate(self,session,user,document_id,enabled):
        source=await self._repository.owned_source(session,user,document_id)
        if source is None:
            raise KnowledgeImportError('KNOWLEDGE_DOCUMENT_NOT_FOUND','资料不存在或无权操作',404)
        if enabled and (self.index_status(source)!='ready' or source['current_version_id']!=source['version_id']
            or not source['total_chunks'] or source['total_chunks']!=source['processed_chunks']
            or source['is_sensitive'] or not source['external_embedding_allowed']):
            raise KnowledgeImportError('KNOWLEDGE_DOCUMENT_NOT_READY','仅索引完成且仍获授权的非敏感资料可以启用',409)
        await self._repository.activation(session,user,document_id,enabled)
        return dict(document_id=document_id,enabled=enabled,index_status=self.index_status(source))

    async def job(self,session,user,job_id):
        return await self._repository.job(session,user,job_id)

    async def retry(self,session,user,job_id):
        return await self._repository.retry(session,user,job_id,getattr(self._settings,'knowledge_index_max_claims',3))
