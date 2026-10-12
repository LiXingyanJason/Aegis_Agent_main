"""日历展示接口的鉴权、参数、数据裁剪与错误回归测试。"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.controller.calendar_controller import router, get_calendar_service
from app.integration.calendar_mcp_client import CalendarMCPError
from app.repository.user_repository import LocalUser
from app.security.dependencies import CurrentUser, get_current_user
from app.service.calendar_service import CalendarService

CONNECTION = UUID("10000000-0000-4000-8000-000000000020")
USER = LocalUser(id=UUID(int=1), tenant_id=UUID(int=2), email="jason@example.com", display_name="Jason")


class FakeRepository:
    """按归属查找连接并提供站内草稿，避免连接真实数据库。"""
    def __init__(self):
        self.scopes = ["calendar.read", "calendar.write"]

    async def connections(self, session, user, connection_id=None):
        assert user == USER
        if connection_id not in (None, CONNECTION):
            return []
        return [dict(id=CONNECTION, provider="google_calendar", scopes=self.scopes,
                     status="active", expires_at=None)]

    async def event_origin(self, *args):
        return {}

    async def drafts(self, session, user, *, param=None, draft_id=None):
        if draft_id and draft_id != UUID(int=3):
            return []
        return [dict(
            draft_id=UUID(int=3), run_id=UUID(int=4), connection_id=CONNECTION, calendar_id="primary",
            title="总结", description="草稿说明", start_at=datetime(2026, 10, 12, 3, tzinfo=UTC),
            end_at=datetime(2026, 10, 12, 4, tzinfo=UTC), version=1, draft_status="pending_approval",
            approval_item_id=UUID(int=5), approval_status="pending", can_confirm=False,
            expires_at=datetime(2026, 10, 11, tzinfo=UTC), timezone="Asia/Shanghai",
            attendees=["jason@example.com"], preview_snapshot={"recommendation": {"reason": "最早空闲"}},
        )]


class FakeClient:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.malformed = False
        self.event = dict(external_event_id="event/1", title="总结",
                          start_at="2026-10-12T03:00:00Z", end_at="2026-10-12T03:50:00Z",
                          attendees=["private@example.com"], description="详情内容")

    async def call(self, name, arguments, **identity):
        self.calls.append((name, arguments, identity))
        if self.fail:
            raise CalendarMCPError("工具不可用")
        if self.malformed:
            return {"source": "mock_calendar", "events": [{}]}
        if name == "calendar.get_event":
            return {"source": "mock_calendar",
                    "event": self.event if arguments["external_event_id"] == "event/1" else None}
        return {"source": "mock_calendar", "events": [self.event]}


class TestService(CalendarService):
    __test__ = False

    async def _read(self, user, method, *args, **kwargs):
        return await method(None, user, *args, **kwargs)


def make_app(authenticated=True):
    """真实 Controller 与 Service 组合，只替换数据库和 MCP 边界。"""
    client = FakeClient()
    service = TestService(None, client, FakeRepository())
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_calendar_service] = lambda: service
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(USER)
    return app, client


def query(**overrides):
    return dict(connection_id=str(CONNECTION), start_at="2026-10-12T00:00:00+08:00",
                end_at="2026-10-13T00:00:00+08:00", **overrides)


def test_calendar_requires_authentication():
    """匿名用户不能读取任何日历资源。"""
    app, _ = make_app(False)
    with TestClient(app) as client:
        assert client.get("/calendar/calendars").status_code == 401


def test_events_timezone_minimal_list_and_precise_detail():
    """事件列表隐藏敏感详情；详情按稳定 ID 读取，UTC 转为北京时间。"""
    app, mcp = make_app()
    with TestClient(app) as client:
        source = client.get("/calendar/calendars").json()["data"][0]
        assert source["source"] == "mock_calendar"
        response = client.get("/calendar/events", params=query())
        assert response.status_code == 200
        event = response.json()["data"]["events"][0]
        assert event["start_at"] == "2026-10-12T11:00:00+08:00"
        assert "attendees" not in event and "description" not in event
        detail = client.get("/calendar/events/detail",
                            params={"connection_id": str(CONNECTION), "external_event_id": "event/1"})
        assert detail.status_code == 200
        assert detail.json()["data"]["description"] == "详情内容"
        assert mcp.calls[-1][2]["user_id"] == USER.id
        assert client.get("/calendar/events/detail", params={
            "connection_id": str(CONNECTION), "external_event_id": "missing"}).status_code == 404


@pytest.mark.parametrize("changes", [
    {"start_at": "2026-10-12T00:00:00"},
    {"end_at": "2026-10-11T00:00:00Z"},
    {"end_at": "2027-10-12T00:00:00Z"},
    {"timezone": "invalid/zone"},
    {"calendar_id": "other"},
])
def test_invalid_range_and_timezone_do_not_call_mcp(changes):
    """非法日期、时区、日历来源在请求层被拒绝。"""
    app, mcp = make_app()
    params = query()
    params.update(changes)
    with TestClient(app) as client:
        assert client.get("/calendar/events", params=params).status_code == 422
    assert not mcp.calls


def test_unowned_connection_and_mcp_failure():
    """越权连接不得进入 MCP；工具故障或非法数据不得伪装成空列表。"""
    app, mcp = make_app()
    with TestClient(app) as client:
        params = query()
        params["connection_id"] = str(UUID(int=99))
        assert client.get("/calendar/events", params=params).status_code == 404
        assert not mcp.calls
        mcp.fail = True
        response = client.get("/calendar/events", params=query())
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "CALENDAR_TOOL_UNAVAILABLE"
        mcp.fail = False
        mcp.malformed = True
        assert client.get("/calendar/events", params=query()).status_code == 503


def test_drafts_are_local_and_detail_is_owned():
    """草稿独立于 MCP，列表不带描述，详情保留推荐及不可确认状态。"""
    app, mcp = make_app()
    with TestClient(app) as client:
        response = client.get("/calendar/drafts", params=query())
        assert response.status_code == 200
        assert "description" not in response.json()["data"][0]
        detail = client.get(f"/calendar/drafts/{UUID(int=3)}")
        assert detail.status_code == 200
        assert detail.json()["data"]["can_confirm"] is False
        assert detail.json()["data"]["recommendation"]["reason"] == "最早空闲"
        assert client.get(f"/calendar/drafts/{UUID(int=99)}").status_code == 404
    assert not mcp.calls


def test_missing_scope_denies_connection_without_tool_call():
    """属于本人但无读取权限的连接返回 409，来源列表中也不显示。"""
    app, mcp = make_app()
    service = app.dependency_overrides[get_calendar_service]()
    service._repository.scopes = ["calendar.write"]
    with TestClient(app) as client:
        assert client.get("/calendar/calendars").json()["data"] == []
        response = client.get("/calendar/events", params=query())
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "CONNECTION_REQUIRED"
    assert not mcp.calls
