"""外部连接与工具调用审计记录的数据访问。"""

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    """当前用户可用于某项外部工具的最小有效连接信息。"""

    id: UUID
    provider: str


class ProviderConnectionRepository:
    """按工具声明查询当前用户有效的外部服务连接。"""

    async def find_active_connection(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        providers: tuple[str, ...],
        required_scope: str | None,
        required_connection_id: UUID | None = None,
    ) -> ProviderConnection | None:
        """返回满足提供商、scope 与可选指定连接标识的有效连接。"""
        if not providers:
            return None
        result = await session.execute(
            text(
                "SELECT id, provider FROM provider_connections "
                "WHERE tenant_id = :tenant_id AND user_id = :user_id "
                "  AND provider = ANY(CAST(:providers AS varchar[])) "
                "  AND status = 'active' "
                "  AND (expires_at IS NULL OR expires_at > now()) "
                "  AND (:connection_id IS NULL OR id = :connection_id) "
                "  AND (CAST(:required_scope AS varchar) IS NULL "
                "       OR scopes @> CAST(:scope_json AS jsonb)) "
                "ORDER BY last_verified_at DESC NULLS LAST, created_at DESC LIMIT 1"
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "providers": list(providers),
                "connection_id": required_connection_id,
                "required_scope": required_scope,
                "scope_json": json.dumps([required_scope]) if required_scope else None,
            },
        )
        row = result.mappings().first()
        return ProviderConnection(id=row["id"], provider=row["provider"]) if row else None


# 兼容旧的导入名称；后续所有新工具均使用 ProviderConnectionRepository。
CalendarConnectionRepository = ProviderConnectionRepository


class ToolCallRepository:
    """保存工具调用的完整审计载荷和安全展示摘要。"""

    async def create_running(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
        step_id: UUID,
        connection_id: UUID | None,
        tool_name: str,
        risk_level: str,
        tool_version: str,
        mcp_server: str,
        request_payload: dict[str, Any],
        input_summary: dict[str, Any],
    ) -> UUID:
        """在发起网络调用前创建 running 工具调用记录。"""
        result = await session.execute(
            text(
                "INSERT INTO tool_calls "
                "(tenant_id, run_id, step_id, connection_id, tool_name, risk_level, tool_version, "
                " mcp_server, request_payload, input_summary, status, started_at) "
                "VALUES (:tenant_id, :run_id, :step_id, :connection_id, :tool_name, :risk_level, "
                "        :tool_version, :mcp_server, CAST(:request_payload AS jsonb), "
                "        CAST(:input_summary AS jsonb), 'running', now()) RETURNING id"
            ),
            {
                "tenant_id": tenant_id,
                "run_id": run_id,
                "step_id": step_id,
                "connection_id": connection_id,
                "tool_name": tool_name,
                "risk_level": risk_level,
                "tool_version": tool_version,
                "mcp_server": mcp_server,
                "request_payload": json.dumps(request_payload, ensure_ascii=False),
                "input_summary": json.dumps(input_summary, ensure_ascii=False),
            },
        )
        return result.mappings().one()["id"]

    async def succeed(
        self,
        session: AsyncSession,
        *,
        tool_call_id: UUID,
        tenant_id: UUID,
        response_payload: dict[str, Any],
        output_summary: dict[str, Any],
        duration_ms: int,
    ) -> None:
        """写入工具响应与安全摘要，并结束调用记录。"""
        await session.execute(
            text(
                "UPDATE tool_calls SET status = 'succeeded', response_payload = CAST(:response_payload AS jsonb), "
                "output_summary = CAST(:output_summary AS jsonb), duration_ms = :duration_ms, finished_at = now() "
                "WHERE id = :tool_call_id AND tenant_id = :tenant_id"
            ),
            {
                "tool_call_id": tool_call_id,
                "tenant_id": tenant_id,
                "response_payload": json.dumps(response_payload, ensure_ascii=False),
                "output_summary": json.dumps(output_summary, ensure_ascii=False),
                "duration_ms": duration_ms,
            },
        )

    async def fail(
        self,
        session: AsyncSession,
        *,
        tool_call_id: UUID,
        tenant_id: UUID,
        error_code: str,
        error_message: str,
        duration_ms: int,
    ) -> None:
        """结束失败的工具调用，记录对页面安全的错误信息。"""
        await session.execute(
            text(
                "UPDATE tool_calls SET status = 'failed', error_code = :error_code, "
                "error_message = :error_message, duration_ms = :duration_ms, finished_at = now() "
                "WHERE id = :tool_call_id AND tenant_id = :tenant_id"
            ),
            {
                "tool_call_id": tool_call_id,
                "tenant_id": tenant_id,
                "error_code": error_code,
                "error_message": error_message[:1000],
                "duration_ms": duration_ms,
            },
        )
