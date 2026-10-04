"""Calendar MCP Server 的 FastAPI 启动入口。"""

from fastapi import FastAPI

from app.controller.health_controller import router as health_router
from app.controller.mcp_controller import router as mcp_router

app = FastAPI(title="Aegis Calendar MCP Server", version="0.1.0")
app.include_router(health_router)
app.include_router(mcp_router)
