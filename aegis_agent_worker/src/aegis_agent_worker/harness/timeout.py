"""聊天、工具和 Embedding 共用的协程超时边界。"""
import asyncio
from contextlib import asynccontextmanager
from aegis_agent_worker.harness.exceptions import HarnessError


@asynccontextmanager
async def execution_timeout(seconds, code="EXECUTION_TIMEOUT", message="任务执行超时"):
    try:
        async with asyncio.timeout(seconds):
            yield
    except TimeoutError as error:
        raise HarnessError(code, message) from error
