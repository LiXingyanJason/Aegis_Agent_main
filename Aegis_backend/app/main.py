"""FastAPI 应用启动入口。"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.controller.conversation_controller import router as conversation_router
from app.config.database import Database
from app.config.redis import create_redis_client
from app.config.settings import get_settings
from app.security.auth_security import OIDCAuthenticator
from app.service.identity_service import IdentityService



@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """读取项目配置，数据库地址、Redis 地址、环境变量。"""
    settings = get_settings()
    database = Database(settings)
    redis = create_redis_client(settings)
    # 将运行期共享资源挂到 app.state，供认证依赖和后续接口使用
    # Request 对象持有对当前 FastAPI 应用的引用：request.app,应用对象保存共享资源：app.state
    app.state.settings = settings
    app.state.database = database
    app.state.redis = redis
    app.state.oidc_authenticator = OIDCAuthenticator(settings)
    app.state.identity_service = IdentityService(settings)
    try:
        yield
    finally:
        await redis.aclose()
        await database.dispose()


app = FastAPI(title="Aegis PA API", version="0.1.0", lifespan=lifespan)
app.include_router(conversation_router)


@app.get("/health", tags=["system"])
async def health_check() -> dict[str, object]:
    """存活检查接口；数据库与 Redis 连通性由独立探针检查。"""
    return {"data": {"status": "ok"}}

