"""日历展示页受保护查询接口。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.common.response import success
from app.integration.calendar_mcp_client import CalendarMCPError
from app.param.calendar_param import (
    CalendarEventsParam, CalendarEventDetailParam, CalendarDraftsParam, CalendarTimezoneParam,
)
from app.security.dependencies import CurrentUser, get_current_user
from app.service.calendar_service import CalendarService, CalendarNotFoundError, CalendarConnectionRequiredError
from app.vo.calendar_vo import (
    CalendarSourceVO, CalendarEventsVO, CalendarEventDetailVO, CalendarDraftVO, CalendarDraftDetailVO,
)

router = APIRouter(prefix="/calendar", tags=["calendar"])


def get_calendar_service(request: Request) -> CalendarService:
    """使用应用启动期装配的日历服务。"""
    return request.app.state.calendar_service


async def _respond(operation):
    """统一映射资源、连接和 MCP 失败，禁止用空列表隐藏故障。"""
    try:
        return success(await operation)
    except CalendarNotFoundError as error:
        raise HTTPException(404, detail=str(error)) from error
    except CalendarConnectionRequiredError as error:
        raise HTTPException(409, detail={"code": "CONNECTION_REQUIRED", "message": str(error),
                                        "required_scopes": ["calendar.read"]}) from error
    except CalendarMCPError as error:
        raise HTTPException(503, detail={"code": "CALENDAR_TOOL_UNAVAILABLE", "message": str(error)}) from error


@router.get("/calendars")
async def list_calendars(current_user: CurrentUser = Depends(get_current_user),
                        service: CalendarService = Depends(get_calendar_service)) -> dict[str, list[CalendarSourceVO]]:
    """列出本人可读取的日历来源。"""
    return await _respond(service.list_calendars(current_user.user))


@router.get("/events")
async def list_events(param: Annotated[CalendarEventsParam, Query()],
                      current_user: CurrentUser = Depends(get_current_user),
                      service: CalendarService = Depends(get_calendar_service)) -> dict[str, CalendarEventsVO]:
    """查询可见范围内正式日程，不返回完整描述与参会人邮箱。"""
    return await _respond(service.list_events(current_user.user, param))


@router.get("/events/detail")
async def event_detail(param: Annotated[CalendarEventDetailParam, Query()],
                       current_user: CurrentUser = Depends(get_current_user),
                       service: CalendarService = Depends(get_calendar_service)) -> dict[str, CalendarEventDetailVO]:
    """按连接、日历、外部 ID 读取详情。"""
    return await _respond(service.event_detail(current_user.user, param))


@router.get("/drafts")
async def list_drafts(param: Annotated[CalendarDraftsParam, Query()],
                      current_user: CurrentUser = Depends(get_current_user),
                      service: CalendarService = Depends(get_calendar_service)) -> dict[str, list[CalendarDraftVO]]:
    """按范围与确认状态读取当前用户站内草稿。"""
    return await _respond(service.list_drafts(current_user.user, param))


@router.get("/drafts/{draft_id}")
async def draft_detail(draft_id: UUID, param: Annotated[CalendarTimezoneParam, Query()],
                       current_user: CurrentUser = Depends(get_current_user),
                       service: CalendarService = Depends(get_calendar_service)) -> dict[str, CalendarDraftDetailVO]:
    """查询草稿内容及其当前版本确认状态。"""
    return await _respond(service.draft_detail(current_user.user, draft_id, param.timezone))
