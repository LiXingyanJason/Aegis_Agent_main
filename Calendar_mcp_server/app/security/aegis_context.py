"""校验并承载主 Aegis 服务传入的受控调用上下文。"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.common.exceptions import MCPError


@dataclass(frozen=True, slots=True)
class AegisContext:
    """Calendar MCP 不自行认证用户，仅接受主服务已校验后的内部标识。"""

    connection_id: UUID
    tenant_id: UUID
    user_id: UUID
    run_id: UUID


def parse_aegis_context(meta: dict[str, Any] | None) -> AegisContext:
    """校验 _meta 中四个 Aegis UUID 字段。"""
    if not isinstance(meta, dict):
        raise MCPError(-32602, "工具参数或 Aegis 上下文无效")
    try:
        return AegisContext(
            connection_id=UUID(str(meta["aegis_connection_id"])),
            tenant_id=UUID(str(meta["aegis_tenant_id"])),
            user_id=UUID(str(meta["aegis_user_id"])),
            run_id=UUID(str(meta["aegis_run_id"])),
        )
    except (KeyError, ValueError, TypeError) as error:
        raise MCPError(-32602, "缺少有效 Aegis 连接与身份上下文") from error
