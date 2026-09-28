"""FastAPI 应用启动入口。"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.config.database import Database
from app.config.redis import create_redis_client
from app.config.settings import get_settings



@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """读取项目配置，数据库地址、Redis 地址、环境变量。"""
    settings = get_settings()
    database = Database(settings)
    redis = create_redis_client(settings)
    # 将资源挂到 app.state，之后可用临时包 fastapi.database.list_users()使用
    app.state.settings = settings
    app.state.database = database
    app.state.redis = redis
    try:
        yield
    finally:
        await redis.aclose()
        await database.dispose()


app = FastAPI(title="Aegis PA API", version="0.1.0", lifespan=lifespan)


@app.get("/health", tags=["system"])
async def health_check() -> dict[str, object]:
    """存活检查接口；数据库与 Redis 连通性由独立探针检查。"""
    return {"data": {"status": "ok"}}

