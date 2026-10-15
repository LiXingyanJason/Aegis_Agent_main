"""独立索引消费进程；Redis唤醒 + SQL租约恢复，不阻塞Agent消费循环。"""
import asyncio
import json
import logging
from time import monotonic
from uuid import UUID
from aegis_agent_worker.config.settings import get_settings
from aegis_agent_worker.config.database import Database
from aegis_agent_worker.config.redis import create_redis_client
from aegis_agent_worker.embedding.bootstrap import create_embedding_executor
from aegis_agent_worker.repository.knowledge_index_repository import KnowledgeIndexRepository
from aegis_agent_worker.storage.local_document_storage import LocalDocumentStorage
from aegis_agent_worker.vector.qdrant_index import QdrantVectorIndex
from aegis_agent_worker.service.knowledge.knowledge_index_service import KnowledgeIndexService
from aegis_agent_worker.observability.structured_logging import configure_structured_logging,log_event
from aegis_agent_worker.observability.telemetry import configure_telemetry,get_tracer

logger=logging.getLogger(__name__)


def parse_message(raw):
    payload=json.loads(raw)
    if not isinstance(payload,dict) or set(payload)!={'job_id','tenant_id','user_id'}:
        raise ValueError('非法索引消息')
    return {key:UUID(value) for key,value in payload.items()}


async def run_worker():
    s=get_settings()
    configure_structured_logging() # 启用结构化日志，使后续日志带有事件名、任务 ID 等字段
    configure_telemetry(s,service_name='aegis-index-worker') # 配置链路追踪，并把这个进程标识为 aegis-index-worker
    database=Database(s)
    redis=create_redis_client(s) # 负责传递待消费的任务消息
    repository=KnowledgeIndexRepository(database) # 负责数据库任务记录
    service=KnowledgeIndexService(s,repository,LocalDocumentStorage(s.knowledge_storage_root,s.knowledge_max_source_bytes),
        create_embedding_executor(s,database),QdrantVectorIndex(s)) # 执行索引、向量化
    # LocalDocumentStorage(...)：读取受控目录中的源文件
    # create_embedding_executor(...)：生成向量，并处理相应的执行治理
    # QdrantVectorIndex(s)：将向量写入 Qdrant
    next_scan=0 # 记录下一次扫描数据库的时间。初值为 0，因此进入循环后会立即进行第一次扫描
    log_event(logger,logging.INFO,'index_worker_started',queue_name=s.knowledge_index_queue_name)
    try:
        while True:
            # 到下次扫描时间 队列没有积压到设定的上限(1000)。
            if monotonic()>=next_scan and await redis.llen(s.knowledge_index_queue_name)<1000:
                # 向数据库查询“可以恢复或补投”的索引任务，逐项处理
                # queued：任务已创建，但可能尚未成功投递 Redis，或消息已丢失。
                # running 且租约(有期限的任务占用权)过期：某个 Worker 领取过任务，但可能中途退出，未能完成或续租。
                for item in await repository.recoverable(s.knowledge_index_recovery_seconds):
                    try: # 把该任务作为 JSON 消息放到 Redis List 尾部。
                        await redis.rpush(s.knowledge_index_queue_name,json.dumps({key:str(value) for key,value in item.items()}))
                    except Exception:
                        log_event(logger,logging.WARNING,'index_dispatch_deferred')
                next_scan=monotonic()+s.knowledge_index_recovery_seconds # 设置下一次扫描时间
            # 从 Redis List 头部取一条任务。队列为空时最多等待 5 秒
            item = await redis.blpop(s.knowledge_index_queue_name,timeout=5)
            if item is None: continue
            try:
                message=parse_message(item[1]) # 校验格式 item[1]是实际消息
            except (ValueError,TypeError):
                log_event(logger,logging.WARNING,'index_queue_message_invalid')
                continue
            try:
                with get_tracer(__name__).start_as_current_span('knowledge.index') as span: # 创建追踪 span
                    span.set_attribute('aegis.job_id',str(message['job_id']))
                    # 真正执行索引
                    result = await service.execute(message)

                log_event(logger,logging.INFO,'knowledge_index_finished',job_id=message['job_id'],**result)
            except Exception:
                # 不打印可能包含原文/参数的SQL或远端异常；过期租约可重领。
                log_event(logger,logging.ERROR,'knowledge_index_unhandled',job_id=message['job_id'])
    finally:
        await redis.aclose()
        await database.dispose()
        log_event(logger,logging.INFO,'index_worker_stopped')


def main():
    asyncio.run(run_worker())


if __name__=='__main__': main()
