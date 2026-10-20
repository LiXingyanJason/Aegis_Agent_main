"""任务运行查询的业务逻辑。"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.run_repository import AgentRunRepository
from app.repository.user_repository import LocalUser
from app.event.run_event import RunEvent


class RunService:
    """处理任务状态、进度步骤等面向页面的读取规则。"""

    def __init__(self, runs: AgentRunRepository | None = None) -> None:
        self._runs = runs or AgentRunRepository()

    async def get_run_detail(
        self, session: AsyncSession, local_user: LocalUser, run_id: UUID
    ) -> dict[str, Any] | None:
        """返回当前用户拥有的任务运行及其步骤；不存在或无权访问时返回 None。"""
        run = await self._runs.find_run_detail_for_user(
            session,
            run_id=run_id,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
        )
        if run is None:
            return None
        run["steps"] = await self._runs.list_steps_for_run_detail(
            session, run_id=run_id, tenant_id=local_user.tenant_id
        )
        return run

    async def ensure_run_owned(
        self, session: AsyncSession, local_user: LocalUser, run_id: UUID
    ) -> bool:
        """确认任务归属当前用户，供 SSE 建立连接前进行一次权限校验。"""
        run = await self._runs.find_run_detail_for_user(
            session,
            run_id=run_id,
            tenant_id=local_user.tenant_id,
            user_id=local_user.id,
        )
        return run is not None

    async def get_mail_tool_results(
        self, session: AsyncSession, local_user: LocalUser, run_id: UUID,
        tool_call_id: UUID, offset: int, limit: int,
    ) -> dict[str, Any] | None:
        """分页返回工具执行时的完整邮件元数据列表；不返回正文或原始载荷。"""
        row = await self._runs.find_mail_tool_result_for_user(
            session, run_id=run_id, tool_call_id=tool_call_id,
            tenant_id=local_user.tenant_id, user_id=local_user.id,
        )
        if row is None:
            return None
        payload = _json_object(row['response_payload'])
        summary = _json_object(row['output_summary'])
        input_summary = _json_object(row['input_summary'])
        messages = payload.get('messages')
        if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
            raise ValueError('邮件工具快照不可读取')
        items = [{
            'sender_name': item.get('sender_name'),
            'sender_email': item.get('sender_email'),
            'subject': item.get('subject'),
            'received_at': item.get('received_at'),
            'list_preview': item.get('list_preview'),
            'attachment_count': item.get('attachment_count'),
        } for item in messages[offset:offset + limit]]
        return {
            'source': payload.get('source'),
            'query_action': input_summary.get('action'),
            'mail_count': len(messages),
            'matched_count': summary.get('matched_count'),
            'offset': offset,
            'limit': limit,
            'items': items,
        }

    async def list_run_events(
        self,
        session: AsyncSession,
        local_user: LocalUser,
        run_id: UUID,
        after_event_no: int,
    ) -> list[RunEvent]:
        """读取当前用户任务的持久事件，用于 SSE 首次连接和断线续传。"""
        return await self._runs.list_events_for_run(
            session,
            run_id=run_id,
            tenant_id=local_user.tenant_id,
            after_event_no=after_event_no,
        )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError('邮件工具快照不可读取')
    return value
