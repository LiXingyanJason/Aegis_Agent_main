"""SQL任务是事实源，Redis仅唤醒；失败由Index Worker扫描补投。"""
import json
import logging
from app.config.database import register_after_commit

logger = logging.getLogger(__name__)


class KnowledgeDispatchService:
    def __init__(self, redis, queue_name):
        self._redis, self._queue = redis, queue_name

    def enqueue_after_commit(self, item, user):
        payload = json.dumps({"job_id":str(item["job_id"]), "tenant_id":str(user.tenant_id), "user_id":str(user.id)})
        async def enqueue():
            try:
                await self._redis.rpush(self._queue, payload)
            except Exception:
                # 不将已提交的导入伪装回滚；SQL扫描可恢复，不输出正文/连接凭据。
                logger.warning("knowledge_dispatch_deferred job_id=%s", item["job_id"])
        register_after_commit(enqueue)
