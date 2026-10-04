"""服务健康检查接口。"""

from fastapi import APIRouter

router = APIRouter(tags=["system"])


@router.get("/health")
async def health_check() -> dict[str, object]:
    """供本地启动、容器健康检查和联调使用。"""
    return {"data": {"status": "ok", "provider": "mock_calendar"}}
