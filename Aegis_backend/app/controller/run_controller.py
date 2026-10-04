"""Agent 任务运行状态查询与 SSE 事件接口。"""

import asyncio
import json
from collections.abc import AsyncIterator

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.common.response import success
from app.config.database import Database, tenant_transaction
from app.event.run_event import RunEvent, run_event_channel
from app.security.dependencies import CurrentUser, get_current_user, get_tenant_session
from app.service.run_service import RunService
from app.vo.run_vo import RunDetailVO, RunStepDetailVO

router = APIRouter(prefix="/runs", tags=["runs"])
_run_service = RunService()

_terminal_event_types = {"run_completed", "run_failed"}


@router.get("/{run_id}")
async def get_run_detail(
    run_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_session),
) -> dict[str, RunDetailVO]:
    """返回当前用户任务的状态、模型信息和执行步骤，供轮询与页面恢复使用。"""
    run = await _run_service.get_run_detail(session, current_user.user, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在或无权访问")

    response = RunDetailVO(
        run_id=run["id"],
        conversation_id=run["conversation_id"],
        status=run["status"],
        current_stage=run["current_stage"],
        model_provider=run["model_provider"],
        model_name=run["model_name"],
        result_summary=run["result_summary"],
        error_code=run["error_code"],
        error_message=run["error_message"],
        started_at=run["started_at"],
        finished_at=run["finished_at"],
        created_at=run["created_at"],
        steps=[
            RunStepDetailVO(
                step_id=step["id"],
                sequence_no=step["sequence_no"],
                step_key=step["step_key"],
                label=step["label"],
                status=step["status"],
                detail=step["detail"],
                started_at=step["started_at"],
                finished_at=step["finished_at"],
            )
            for step in run["steps"]
        ],
    )
    return success(response)


@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """以 SSE 补发并实时推送当前用户任务的进度和最终结果事件。"""
    after_event_no = _parse_last_event_id(last_event_id)
    database: Database = request.app.state.database
    async with database.session() as session:
        async with tenant_transaction(session, current_user.tenant_context):
            is_owned = await _run_service.ensure_run_owned(session, current_user.user, run_id)
    if not is_owned:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在或无权访问")

    return StreamingResponse(
        _run_event_stream(
            request=request,
            database=database,
            redis=request.app.state.redis,
            run_id=run_id,
            current_user=current_user,
            after_event_no=after_event_no,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _parse_last_event_id(value: str | None) -> int:
    """解析浏览器断线续传的 Last-Event-ID；未提供时从首条事件开始。"""
    if value is None or value == "":
        return 0
    try:
        event_no = int(value)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Last-Event-ID 必须为非负整数") from error
    if event_no < 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Last-Event-ID 必须为非负整数")
    return event_no


async def _run_event_stream(
    *,
    request: Request,
    database: Database,
    redis,
    run_id: UUID,
    current_user: CurrentUser,
    after_event_no: int,
) -> AsyncIterator[str]:
    """先订阅实时通道再补发数据库事件，避免订阅期间遗漏已提交事件。"""
    pubsub = redis.pubsub()
    await pubsub.subscribe(run_event_channel(run_id))
    last_sent_event_no = after_event_no
    try:
        # 订阅成功后再读取持久历史：期间到达的实时消息会留在 Pub/Sub 缓冲中，后续按 event_no 去重。
        async with database.session() as session:
            async with tenant_transaction(session, current_user.tenant_context):
                history = await _run_service.list_run_events(
                    session, current_user.user, run_id, after_event_no
                )
        for event in history:
            yield _format_sse_event(event)
            last_sent_event_no = event.event_no
            if event.event_type in _terminal_event_types:
                return

        while not await request.is_disconnected():
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
            if message is None:
                yield ": keepalive\n\n"
                continue
            raw_payload = message["data"]
            if not isinstance(raw_payload, str):
                continue
            event = RunEvent.from_wire_payload(raw_payload)
            if event.event_no <= last_sent_event_no:
                continue
            yield _format_sse_event(event)
            last_sent_event_no = event.event_no
            if event.event_type in _terminal_event_types:
                return
    except asyncio.CancelledError:
        raise
    finally:
        await pubsub.unsubscribe(run_event_channel(run_id))
        await pubsub.aclose()


def _format_sse_event(event: RunEvent) -> str:
    """将一个运行事件编码为浏览器 EventSource 可识别的 SSE 帧。"""
    data = json.dumps(
        {
            "run_id": str(event.run_id),
            "event_no": event.event_no,
            "payload": event.payload,
            "created_at": event.created_at.isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event.event_no}\nevent: {event.event_type}\ndata: {data}\n\n"
