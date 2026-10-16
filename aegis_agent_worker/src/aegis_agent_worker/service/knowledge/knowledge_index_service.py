"""确定性文档索引：不经过聊天主Agent，不执行文档中的指令。"""
from hashlib import sha256
from uuid import uuid4
from aegis_agent_worker.embedding.contracts import EmbeddingProfile
from aegis_agent_worker.harness.embedding_executor import EmbeddingSubject
from aegis_agent_worker.harness.exceptions import HarnessError
from aegis_agent_worker.indexing.chunker import chunk_text,PARSER_VERSION


class KnowledgeIndexService:
    def __init__(self,settings,repository,storage,embedding,index):
        self._settings,self._repository,self._storage,self._embedding,self._index=settings,repository,storage,embedding,index

    async def execute(self,message):
        token = uuid4() # 生成本次领取专用的随机租约令牌
        s = self._settings
        # 尝试在数据库中领取任务：检查它是否可领取、是否超过次数上限，然后将其标为 running，记录令牌和租约到期时间。
        job = await  self._repository.claim(message,token,s.knowledge_index_lease_seconds,s.knowledge_index_max_claims)
        if job is None:
            return {'status':'ignored'} # 没有领取成功就忽略这条 Redis 消息。常见原因是重复投递、任务已完成，或者其他 Worker 持有有效租约
        try:
            job = await self._repository.load(message,token)
            if job['is_sensitive'] or not job['external_embedding_allowed']:
                raise HarnessError('KNOWLEDGE_DATA_POLICY_BLOCKED','资料不允许发送到外部服务')
            # 核对 API 创建索引任务时固定的模型、版本和维度，是否与当前 Worker 的配置完全一致
            profile=EmbeddingProfile(job['embedding_provider'],job['embedding_model'],job['embedding_model_version'],job['embedding_dimensions'])
            if profile!=EmbeddingProfile(s.embedding_provider,s.embedding_model,s.embedding_model_version,s.embedding_dimensions):
                raise HarnessError('EMBEDDING_PROFILE_MISMATCH','API登记的模型空间与Worker配置不一致')
            # 兼容两种来源：旧资料的数据库内联原文，以及新资料的本地源文件引用。
            if job.get('source_text') is not None:
                raw=job['source_text'].encode('utf-8')
            elif job['storage_backend']=='local':
                raw=await self._storage.read(job['storage_key'])
            else:
                raise HarnessError('KNOWLEDGE_STORAGE_UNAVAILABLE','源文件存储类型暂不支持')
            # 计算实际文件的 SHA-256，与登记时的哈希比较，确认内容没有变化
            if sha256(raw).hexdigest()!=job['source_hash']:
                raise HarnessError('KNOWLEDGE_SOURCE_CHANGED','源文件hash与登记记录不一致')
            # 检查文件非空、没有超出大小限制，并且字节数与数据库记录一致
            if not raw or len(raw)>s.knowledge_max_source_bytes or (job.get('source_bytes') is not None and len(raw)!=job['source_bytes']):
                raise HarnessError('KNOWLEDGE_SOURCE_INVALID','源文件大小不一致或超限')
            try:
                chunks=chunk_text(raw.decode('utf-8-sig'),job['version_id']) # 将 UTF-8 原文解析并切成多个片段。
            except (ValueError,UnicodeError):
                raise HarnessError('KNOWLEDGE_PARSE_FAILED','源文件解析或切块失败') from None
            # 检查 KnowledgeIndexService 客户端配置，并确认 Qdrant 集合可用、与所需向量维度匹配。
            self._embedding.ensure_configured()
            current = await self._index.ensure_collection(profile)
            # 数据库中固定“这个向量空间对应哪个集合和模型配置”。已有集合与配置冲突时，不能直接混写
            await self._repository.bind_space(self._index.namespace,s.qdrant_collection,profile,current.get('points_count'))
            # 把片段记录准备到数据库，并查出此前已经确认索引完成的片段
            indexed=await self._repository.prepare_chunks(message,token,job,chunks,self._index.namespace,s.qdrant_collection,PARSER_VERSION)
            # pending 只包含尚未确认完成的片段
            pending=[item for item in chunks if item.id not in indexed]
            # 取各项批量上限中最小的一个，确保每次 Embedding 调用不超限。
            batch_size=min(s.embedding_max_batch_items,self._embedding.max_batch_items,16)

            for offset in range(0,len(pending),batch_size): # 逐批处理待索引片段。
                # 每批开始前先续租，再重新读取数据库中的资料权限。
                await self._repository.renew(message,token,s.knowledge_index_lease_seconds)
                # 再次读取权限事实，不把旧任务快照当作永久外发授权。
                fresh=await self._repository.load(message,token)
                if fresh['is_sensitive'] or not fresh['external_embedding_allowed']:
                    raise HarnessError('KNOWLEDGE_DATA_POLICY_BLOCKED','资料外发授权已撤销')
                batch=pending[offset:offset+batch_size]
                # 取当前批次的片段文本，调用 Embedding 服务生成向量。
                vectors=await self._embedding.embed(EmbeddingSubject('job',job['id'],message['tenant_id'],message['user_id']),[item.content for item in batch])
                # 将片段 ID、对应向量和必要元数据组成 Qdrant 点。
                points=[{'id':str(chunk.id),'vector':vector,'payload':{
                    'tenant_id':str(message['tenant_id']),'owner_id':str(message['user_id']),
                    'document_id':str(job['document_id']),'version_id':str(job['version_id']),
                    'chunk_id':str(chunk.id),'content_hash':chunk.content_hash,
                    'visibility':fresh['visibility'],
                    'model_provider':profile.provider,'model':profile.model,'model_version':profile.version,
                    'dimensions':profile.dimensions}} for chunk,vector in zip(batch,vectors)]
                # 先等待 Qdrant 确认写入，再在数据库中把这一批标为已索引。
                await self._index.upsert(points)
                await self._repository.mark_batch(message,token,job,[item.id for item in batch])
            # 按顺序确认 + SQL 记录进度 + 幂等重试 两行之间失败时，Embedding 可能重复调用并产生额外费用
            await self._repository.complete(message,token,job)
            return {'status':'succeeded','total_chunks':len(chunks)}
        except Exception as error:
            code=getattr(error,'code','KNOWLEDGE_INDEX_FAILED')
            await self._repository.fail(message,token,job,code)
            return {'status':'failed','error_code':code}
        # CancelledError不改为failed；留下租约，由崩溃恢复领取。旧token不能完成新领取任务。
