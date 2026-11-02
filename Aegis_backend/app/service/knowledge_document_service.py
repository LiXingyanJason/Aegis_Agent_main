"""资料查看/软删除及单个证据片段读取；不执行LLM或索引。"""
from app.repository.knowledge_document_repository import KnowledgeDocumentRepository


class KnowledgeDocumentService:
    def __init__(self,repository=None):
        self._repository=repository or KnowledgeDocumentRepository()

    async def detail(self,session,user,document_id):
        return await self._repository.detail(session,user,document_id)

    async def delete(self,session,user,document_id):
        return await self._repository.delete(session,user,document_id)

    async def source(self,session,user,document_id,version_id,chunk_id,content_hash):
        return await self._repository.chunk(session,user,document_id,version_id,chunk_id,content_hash)

    async def generation_source(self,session,user,document_id,version_id,generation_id,chunk_id,content_hash):
        from app.repository.knowledge_source_repository import KnowledgeSourceRepository
        rows=await KnowledgeSourceRepository().load_visible_v2(session,user,[chunk_id])
        return next((dict(r) for r in rows if (r['document_id'],r['version_id'],r['generation_id'],r['content_hash'])==
            (document_id,version_id,generation_id,content_hash)),None)
