"""日历展示页的只读业务服务，不创建 Agent 任务。"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.config.database import TenantContext, tenant_transaction
from app.integration.calendar_mcp_client import CalendarMCPError, CalendarMCPList, CalendarMCPDetail
from app.repository.calendar_repository import CalendarRepository
from app.vo.calendar_vo import (
    CalendarSourceVO, CalendarEventsVO, CalendarEventVO, CalendarEventDetailVO,
    CalendarDraftVO, CalendarDraftDetailVO,
)


class CalendarNotFoundError(RuntimeError):
    """资源不存在或不属于当前用户。"""


class CalendarConnectionRequiredError(RuntimeError):
    """当前用户连接已失效或缺少读取权限。"""


class CalendarService:
    def __init__(self, database, client, repository=None):
        self._database = database
        self._client = client
        self._repository = repository or CalendarRepository()

    async def _read(self, user, method, *args, **kwargs):
        """每次数据库读取单独建立 RLS 短事务。"""
        async with self._database.session() as session:
            async with tenant_transaction(session, TenantContext(tenant_id=user.tenant_id, user_id=user.id)):
                return await method(session, user, *args, **kwargs)

    @staticmethod
    def _is_readable(connection):
        return (connection["status"] == "active"
                and (connection["expires_at"] is None or connection["expires_at"] > datetime.now(UTC))
                and "calendar.read" in connection["scopes"])

    async def list_calendars(self, user):
        connections = await self._read(user, self._repository.connections)
        # 当前 Calendar MCP 仅有 Mock Provider；来源名称不得冒充真实 Google 同步。
        return [CalendarSourceVO(connection_id=item["id"], provider=item["provider"],
                                 can_create_with_approval="calendar.write" in item["scopes"])
                for item in connections if self._is_readable(item)]

    async def _require_connection(self, user, connection_id):
        rows = await self._read(user, self._repository.connections, connection_id)
        if not rows:
            raise CalendarNotFoundError("日历连接不存在或无权访问")
        if not self._is_readable(rows[0]):
            raise CalendarConnectionRequiredError("日历连接无效或缺少 calendar.read 权限")

    async def list_events(self, user, param):
        await self._require_connection(user, param.connection_id)
        raw = await self._client.call("calendar.list_events", {
            "start_at": param.start_at.isoformat(), "end_at": param.end_at.isoformat(),
        }, connection_id=param.connection_id, tenant_id=user.tenant_id, user_id=user.id)
        try:
            result = CalendarMCPList.model_validate(raw)
        except ValidationError as error:
            raise CalendarMCPError("日历列表响应格式无效") from error
        events = sorted((item for item in result.events
                         if item.start_at < param.end_at and item.end_at > param.start_at),
                        key=lambda item: (item.start_at, item.end_at, item.external_event_id))
        zone = ZoneInfo(param.timezone)
        return CalendarEventsVO(
            connection_id=param.connection_id, source=result.source, timezone=param.timezone,
            query_range={"start_at": param.start_at.astimezone(zone), "end_at": param.end_at.astimezone(zone)},
            fetched_at=datetime.now(UTC), events=[self._event_vo(item, zone) for item in events],
        )

    @staticmethod
    def _event_vo(item, zone):
        return CalendarEventVO(external_event_id=item.external_event_id, title=item.title,
                               start_at=item.start_at.astimezone(zone), end_at=item.end_at.astimezone(zone),
                               attendee_count=len(item.attendees))

    async def event_detail(self, user, param):
        await self._require_connection(user, param.connection_id)
        raw = await self._client.call("calendar.get_event", {"external_event_id": param.external_event_id},
                                      connection_id=param.connection_id, tenant_id=user.tenant_id, user_id=user.id)
        try:
            result = CalendarMCPDetail.model_validate(raw)
        except ValidationError as error:
            raise CalendarMCPError("日历详情响应格式无效") from error
        if result.event is None:
            raise CalendarNotFoundError("日程不存在或无权访问")
        if result.event.external_event_id != param.external_event_id:
            raise CalendarMCPError("日历详情返回了不匹配的事件")
        origin = await self._read(user, self._repository.event_origin, param.connection_id,
                                  param.calendar_id, param.external_event_id)
        return CalendarEventDetailVO(
            **self._event_vo(result.event, ZoneInfo(param.timezone)).model_dump(),
            connection_id=param.connection_id, source=result.source, timezone=param.timezone,
            description=result.event.description, location=result.event.location,
            attendees=result.event.attendees, **origin,
        )

    async def list_drafts(self, user, param):
        rows = await self._read(user, self._repository.drafts, param=param)
        return [self._draft_vo(row, param.timezone) for row in rows]

    @staticmethod
    def _draft_vo(row, timezone):
        data = dict(row)
        zone = ZoneInfo(timezone)
        data["start_at"] = row["start_at"].astimezone(zone)
        data["end_at"] = row["end_at"].astimezone(zone)
        return CalendarDraftVO.model_validate(data)

    async def draft_detail(self, user, draft_id, timezone):
        rows = await self._read(user, self._repository.drafts, draft_id=draft_id)
        if not rows:
            raise CalendarNotFoundError("会议草稿不存在或无权访问")
        row = rows[0]
        preview = row.get("preview_snapshot") or {}
        return CalendarDraftDetailVO(
            **self._draft_vo(row, timezone).model_dump(), timezone=timezone,
            description=row["description"], attendees=row["attendees"],
            recommendation=preview.get("recommendation"),
        )
