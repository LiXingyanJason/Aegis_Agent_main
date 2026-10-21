"""会议安排草稿与逐项确认的应用服务。"""

import json
from datetime import datetime, timedelta
from time import monotonic
from zoneinfo import ZoneInfo
from typing import Any
from uuid import UUID

from sqlalchemy import text

from aegis_agent_worker.agent.contracts.calendar import (
    CalendarFreeTimeCandidate,
    CalendarMeetingDraftProposal,
    CalendarDecision,
    extract_calendar_free_time_candidates,
)
from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.event.run_event import RunEventPublisher
from aegis_agent_worker.repository.run_repository import AgentRunRepository, ClaimedRun
from aegis_agent_worker.repository.tool_repository import ToolCallRepository
from aegis_agent_worker.repository.calendar_draft_repository import (
    CalendarDraftRepository, create_calendar_draft as _create_draft, create_calendar_approval as _create_approval_item,
)
from aegis_agent_worker.service.calendar.calendar_tool_execution_service import CalendarToolExecutionService
from aegis_agent_worker.tool.contracts import ToolError, ToolInvocation
from aegis_agent_worker.tool.gateway import ToolGateway


class CalendarWriteError(ToolError):
    """已批准日历写入未完成时抛出的受控错误。"""
    def __init__(self, message, code="CALENDAR_WRITE_FAILED"):
        super().__init__(code, message)


class CalendarConfirmationService:
    """将可用时间查询结果转换为持久化会议草稿和待确认操作。

    此服务只创建草稿，绝不调用 ``calendar.create_event``。外部写入必须等批准后
    由恢复工作流执行。
    """

    def __init__(
        self,
        database: Database,
        runs: AgentRunRepository,
        tool_execution: CalendarToolExecutionService | None,
        events: RunEventPublisher | None = None,
        tools: ToolGateway | None = None,
        tool_calls: ToolCallRepository | None = None,
    ) -> None:
        self._database = database
        self._runs = runs
        self._tool_execution = tool_execution
        self._events = events
        self._tools = tools
        self._tool_calls = tool_calls or ToolCallRepository()

    async def query_available_slots(
        self,
        run: ClaimedRun,
        *,
        invocation: ToolInvocation,
        decision: CalendarDecision | None = None,
    ) -> dict[str, Any]:
        """查询可信空闲候选；本方法不选择时段，也不创建草稿。"""
        if self._tool_execution is None:
            return {"confirmation_error": "当前日历工具尚未配置，无法查询可用时间。"}

        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        execution = await self._tool_execution.execute_read_tool(run, context, invocation)
        if decision and decision.time_mode == "explicit_interval":
            if execution.status != "succeeded":
                return {"confirmation_error": "日历查询不可用，无法核验指定时段，未创建草稿。"}
            try:
                summary = explicit_interval_availability(decision, execution.output_summary)
            except ValueError:
                return {"confirmation_error": "日历返回的时段数据无法核验，未创建草稿。"}
            if not summary["available_slots"]:
                return {"confirmation_error": "指定时段与已有日程冲突，请选择其他时间，未创建草稿。"}
            candidates, duration_minutes = extract_calendar_free_time_candidates(summary)
            return {"availability": {
                "duration_minutes": duration_minutes,
                "candidates": [item.model_dump(mode="json") for item in candidates],
                "scheduling_warning": summary.get("scheduling_warning"),
            }, "tool_context": json.dumps(summary, ensure_ascii=False)}
        candidates, duration_minutes = extract_calendar_free_time_candidates(
            execution.output_summary
        )
        if execution.status != "succeeded" or not candidates:
            return {
                "confirmation_error": "未查询到可用于安排会议的空闲时间，未创建草稿。",
                "tool_context": execution.tool_context,
            }
        return {
            "availability": {
                "duration_minutes": duration_minutes,
                "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
            },
            "tool_context": execution.tool_context,
        }

    async def create_recommended_draft(
        self,
        run: ClaimedRun,
        *,
        proposal: CalendarMeetingDraftProposal,
        selected_slot: CalendarFreeTimeCandidate,
        alternatives: list[CalendarFreeTimeCandidate],
        duration_minutes: int,
        timezone: str = "Asia/Shanghai",
        scheduling_warning: str | None = None,
    ) -> dict[str, Any]:
        """将已经过候选集合校验的推荐时段固化为指定时长的待确认会议草稿。"""
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        start_at, end_at = _meeting_interval(selected_slot, duration_minutes)
        recommendation_preview = {
            "selected_slot_id": selected_slot.slot_id,
            "reason": proposal.reason + (f"\n{scheduling_warning}" if scheduling_warning else ""),
            "scheduling_warning": scheduling_warning,
            "alternative_slots": [
                {
                    "slot_id": candidate.slot_id,
                    "start_at": alternative_start.isoformat(),
                    "end_at": alternative_end.isoformat(),
                    "start_at_local": alternative_start.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M"),
                    "end_at_local": alternative_end.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M"),
                }
                for candidate in alternatives
                for alternative_start, alternative_end in [_meeting_interval(candidate, duration_minutes)]
            ],
        }
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                connection_id = await _find_calendar_connection(session, run.tenant_id, run.user_id)
                if connection_id is None:
                    return {"confirmation_error": "当前用户没有有效日历连接，未创建草稿。"}
                draft_id = await _create_draft(
                    session,
                    run=run,
                    connection_id=connection_id,
                    title=proposal.title,
                    start_at=start_at,
                    end_at=end_at,
                    attendees=proposal.attendees,
                    description=proposal.description,
                    timezone=timezone,
                )
                approval_item_id = await _create_approval_item(
                    session,
                    run=run,
                    draft_id=draft_id,
                    title=proposal.title,
                    start_at=start_at,
                    end_at=end_at,
                    attendees=proposal.attendees,
                    recommendation_preview=recommendation_preview,
                    timezone=timezone,
                )
                await session.execute(
                    text(
                        "UPDATE agent_runs SET status = 'waiting_confirmation', "
                        "current_stage = '等待用户确认会议安排', updated_at = now() "
                        "WHERE id = :run_id AND tenant_id = :tenant_id"
                    ),
                    {"run_id": run.id, "tenant_id": run.tenant_id},
                )
                if self._events is not None:
                    event = await self._runs.append_event(
                        session,
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        event_type="approval_required",
                        payload={
                            "approval_item_id": str(approval_item_id),
                            "resource_type": "calendar_event_draft",
                            "resource_id": str(draft_id),
                            "title": proposal.title,
                            "action": "calendar.create_event",
                            "status": "pending",
                            "preview": {
                                "start_at": start_at.isoformat(),
                                "end_at": end_at.isoformat(),
                                "attendees": proposal.attendees,
                                "timezone": timezone,
                                "scheduling_warning": scheduling_warning,
                                "recommendation": recommendation_preview,
                            },
                        },
                    )
                    self._events.publish_after_commit(event)
        return {
            "awaiting_confirmation": True,
            "draft_id": str(draft_id),
            "approval_item_id": str(approval_item_id),
            "assistant_content": "我已基于可用时间生成会议草稿和备选时段，请在确认页逐项确认后再创建日历事件。",
        }

    async def create_approved_event(
        self, run: ClaimedRun, *, draft_id: UUID, approval_item_id: UUID
    ) -> str:
        """二次核验批准记录后，调用唯一允许的写工具并持久化执行结果。"""
        if self._tools is None:
            raise CalendarWriteError("日历写入工具尚未配置")
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        started_at = monotonic()
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                records=CalendarDraftRepository()
                completed=await records.completed_execution(session,run,draft_id,approval_item_id)
                if completed:
                    return completed
                draft = await _load_approved_draft(
                    session, run=run, draft_id=draft_id, approval_item_id=approval_item_id
                )
                if draft is None:
                    raise CalendarWriteError("会议草稿未获批准、已失效或不属于当前任务", "CALENDAR_APPROVAL_INVALID")
                if await records.has_execution(session,run,approval_item_id):
                    raise CalendarWriteError("已有外部执行记录，请先核对结果，不自动重复写入", "CALENDAR_EXECUTION_UNCERTAIN")
                attendees = await _load_attendees(session, run.tenant_id, draft_id)
                request_payload = {
                    "title": draft["title"], "description": draft["description"],
                    "start_at": draft["start_at"].isoformat(), "end_at": draft["end_at"].isoformat(),
                    "attendees": attendees, "idempotency_key": f"calendar:{approval_item_id}:1",
                    "timezone": draft["timezone"],
                }
                definition = self._tools.get_definition("calendar.create_event")
                step_id = await self._runs.create_running_tool_step(
                    session, run_id=run.id, tenant_id=run.tenant_id,
                    step_key="calendar_create_event", label="正在创建日历事件",
                )
                prepared = await self._tools.prepare(
                    session, tenant_id=run.tenant_id, user_id=run.user_id, run_id=run.id,
                    invocation=ToolInvocation("calendar.create_event", request_payload),
                    allow_confirmed_write=True,
                    required_connection_id=draft["connection_id"],
                )
                execution_id = await _create_execution(
                    session, run=run, approval_item_id=approval_item_id,
                    connection_id=prepared.context.connection_id, request_payload=request_payload,
                )
                tool_call_id = await self._tool_calls.create_running(
                    session, tenant_id=run.tenant_id, run_id=run.id, step_id=step_id,
                    connection_id=prepared.context.connection_id, tool_name=definition.name,
                    risk_level=definition.risk_level, tool_version=definition.version,
                    mcp_server=definition.mcp_server, request_payload=request_payload,
                    input_summary={"title": draft["title"], "attendee_count": len(attendees)},
                )
                if self._events is not None:
                    event = await self._runs.append_event(
                        session, run_id=run.id, tenant_id=run.tenant_id,
                        event_type="progress_updated",
                        payload={"step_id": str(step_id), "label": "正在创建日历事件", "status": "running"},
                    )
                    self._events.publish_after_commit(event)

        try:
            result = await self._tools.invoke(prepared)
        except ToolError as error:
            await self._mark_write_failed(
                run, context, approval_item_id, execution_id, step_id, tool_call_id, error
            )
            raise CalendarWriteError(error.message, error.code) from error

        duration_ms = int((monotonic() - started_at) * 1000)
        external_event_id = str(result.response_payload.get("external_event_id", ""))
        if not external_event_id:
            await self._mark_write_failed(
                run, context, approval_item_id, execution_id, step_id, tool_call_id,
                ToolError("TOOL_INVALID_RESPONSE", "日历服务未返回外部事件标识"),
            )
            raise CalendarWriteError("日历服务未返回外部事件标识", "TOOL_INVALID_RESPONSE")
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                await self._tool_calls.succeed(
                    session, tool_call_id=tool_call_id, tenant_id=run.tenant_id,
                    response_payload=result.response_payload, output_summary=result.output_summary,
                    duration_ms=duration_ms,
                )
                await self._runs.finish_step(
                    session, step_id=step_id, run_id=run.id, tenant_id=run.tenant_id,
                    status="succeeded", detail="日历事件已创建",
                )
                await session.execute(
                    text("UPDATE external_action_executions SET status = 'succeeded', provider_resource_id = :external_event_id, result_summary = CAST(:result AS jsonb), finished_at = now() WHERE id = :execution_id"),
                    {"execution_id": execution_id, "external_event_id": external_event_id, "result": json.dumps(result.output_summary, ensure_ascii=False)},
                )
                await session.execute(
                    text("UPDATE calendar_event_drafts SET status = 'executed', updated_at = now() WHERE id = :draft_id AND tenant_id = :tenant_id"),
                    {"draft_id": draft_id, "tenant_id": run.tenant_id},
                )
                await session.execute(
                    text("UPDATE approval_items SET status = 'executed', updated_at = now() WHERE id = :approval_item_id AND tenant_id = :tenant_id"),
                    {"approval_item_id": approval_item_id, "tenant_id": run.tenant_id},
                )
                if self._events is not None:
                    preview = await self._runs.append_event(session, run_id=run.id, tenant_id=run.tenant_id, event_type="tool_preview", payload={"tool_call_id": str(tool_call_id), "tool_name": "calendar.create_event", "risk_level": "write", "output_summary": result.output_summary, "status": "succeeded"})
                    progress = await self._runs.append_event(session, run_id=run.id, tenant_id=run.tenant_id, event_type="progress_updated", payload={"step_id": str(step_id), "label": "日历事件已创建", "status": "succeeded"})
                    approval_executed = await self._runs.append_event(session, run_id=run.id, tenant_id=run.tenant_id, event_type="approval_executed", payload={"approval_item_id": str(approval_item_id), "resource_type": "calendar_event_draft", "resource_id": str(draft_id), "provider_resource_id": external_event_id, "status": "executed"})
                    self._events.publish_after_commit(preview)
                    self._events.publish_after_commit(progress)
                    self._events.publish_after_commit(approval_executed)
        return external_event_id

    async def _mark_write_failed(self, run: ClaimedRun, context: TenantContext, approval_item_id: UUID, execution_id: UUID, step_id: UUID, tool_call_id: UUID, error: ToolError) -> None:
        """写工具失败时结束审计、步骤、确认项和外部执行记录。"""
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                await self._tool_calls.fail(session, tool_call_id=tool_call_id, tenant_id=run.tenant_id, error_code=error.code, error_message=error.message, duration_ms=0)
                await self._runs.finish_step(session, step_id=step_id, run_id=run.id, tenant_id=run.tenant_id, status="failed", detail=error.message)
                await session.execute(text("UPDATE external_action_executions SET status = 'failed', error_code = :code, error_message = :message, finished_at = now() WHERE id = :execution_id"), {"execution_id": execution_id, "code": error.code, "message": error.message[:1000]})
                await session.execute(text("UPDATE approval_items SET status = 'failed', updated_at = now() WHERE id = :approval_item_id AND tenant_id = :tenant_id"), {"approval_item_id": approval_item_id, "tenant_id": run.tenant_id})


async def _find_calendar_connection(session, tenant_id: UUID, user_id: UUID) -> UUID | None:
    result = await session.execute(
        text(
            "SELECT id FROM provider_connections WHERE tenant_id = :tenant_id "
            "AND user_id = :user_id AND provider IN ('google_calendar', 'outlook_calendar') "
            "AND status = 'active' AND (expires_at IS NULL OR expires_at > now()) "
            "AND scopes @> '[\"calendar.write\"]'::jsonb "
            "ORDER BY last_verified_at DESC NULLS LAST, created_at DESC LIMIT 1"
        ),
        {"tenant_id": tenant_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return row["id"] if row else None


def _meeting_interval(
    candidate: CalendarFreeTimeCandidate, duration_minutes: int
) -> tuple[datetime, datetime]:
    """从空闲区间起点截取用户请求时长，避免把整段空闲时间误建成会议。"""
    if not 15 <= duration_minutes <= 480:
        raise ValueError("会议时长必须在 15 至 480 分钟之间")
    start_at = datetime.fromisoformat(candidate.start_at)
    available_end_at = datetime.fromisoformat(candidate.end_at)
    end_at = start_at + timedelta(minutes=duration_minutes)
    if end_at > available_end_at:
        raise ValueError("推荐时段不足以容纳请求的会议时长")
    return start_at, end_at


def explicit_interval_availability(decision: CalendarDecision, summary) -> dict:
    """对已查询的指定范围做确定性冲突检查，不受自动推荐工作时段限制。"""
    if not isinstance(summary, dict) or not isinstance(summary.get("events"), list):
        raise ValueError("缺少可信日程列表")
    query_range = summary.get("query_range", {})
    if not isinstance(query_range, dict) or not all(isinstance(query_range.get(key), str) for key in ("start_at", "end_at")):
        raise ValueError("日历查询范围格式无效")
    if (datetime.fromisoformat(query_range.get("start_at", "")) != decision.start_at
            or datetime.fromisoformat(query_range.get("end_at", "")) != decision.end_at):
        raise ValueError("日历查询范围与指定范围不一致")
    conflict = False
    for event in summary["events"]:
        try:
            start = datetime.fromisoformat(event["start_at"])
            end = datetime.fromisoformat(event["end_at"])
            if start.tzinfo is None or end.tzinfo is None or end <= start:
                raise ValueError("日程时间无效")
            conflict = conflict or (start < decision.end_at and end > decision.start_at)
        except (KeyError, TypeError):
            raise ValueError("日程格式无效") from None
    if summary.get("events_total", len(summary["events"])) > len(summary["events"]):
        raise ValueError("日程摘要被截断，不能可靠核验")
    zone = ZoneInfo(decision.timezone)
    start, end = decision.start_at.astimezone(zone), decision.end_at.astimezone(zone)
    warning = None
    if start.hour < 9 or end.hour > 18 or (end.hour == 18 and (end.minute or end.second)) or start.date() != end.date():
        warning = "你指定的会议位于非工作时段，请在批准前核对。"
    return {
        "duration_minutes": decision.duration_minutes, "display_timezone": decision.timezone,
        "scheduling_warning": warning,
        "available_slots": [] if conflict else [{
            "start_at": start.isoformat(), "end_at": end.isoformat(),
            "start_at_local": start.strftime("%Y-%m-%d %H:%M"),
            "end_at_local": end.strftime("%Y-%m-%d %H:%M"),
        }],
    }


async def _load_approved_draft(session, *, run: ClaimedRun, draft_id: UUID, approval_item_id: UUID):
    """锁定草稿与确认项，并确认批准决定与当前任务、租户完全一致。"""
    result = await session.execute(
        text(
            "SELECT d.title, d.description, d.start_at, d.end_at, d.timezone, d.connection_id "
            "FROM calendar_event_drafts d JOIN approval_items ai ON ai.resource_id = d.id "
            "JOIN approval_decisions ad ON ad.approval_item_id = ai.id "
            "WHERE d.id = :draft_id AND d.run_id = :run_id AND d.tenant_id = :tenant_id "
            "AND d.user_id=:user_id AND d.status='pending_approval' "
            "AND ai.id = :approval_item_id AND ai.run_id = :run_id "
            "AND ai.tenant_id=d.tenant_id AND ai.user_id=d.user_id AND ai.draft_version=d.version "
            "AND ai.resource_type='calendar_event_draft' AND ai.action='calendar.create_event' "
            "AND ai.expires_at>now() AND ad.tenant_id=d.tenant_id AND ad.actor_user_id=d.user_id AND ad.checked=true "
            "AND ai.status = 'approved_executing' AND ad.decision = 'approved' "
            "FOR UPDATE OF d, ai"
        ),
        {"draft_id": draft_id, "approval_item_id": approval_item_id, "run_id": run.id,
         "tenant_id": run.tenant_id, "user_id": run.user_id},
    )
    return result.mappings().first()


async def _load_attendees(session, tenant_id: UUID, draft_id: UUID) -> list[str]:
    result = await session.execute(
        text("SELECT email FROM calendar_draft_attendees WHERE tenant_id = :tenant_id AND draft_id = :draft_id ORDER BY email"),
        {"tenant_id": tenant_id, "draft_id": draft_id},
    )
    return [row["email"] for row in result.mappings().all()]


async def _create_execution(session, *, run: ClaimedRun, approval_item_id: UUID, connection_id: UUID | None, request_payload: dict[str, Any]) -> UUID:
    """先登记外部副作用及稳定幂等键，网络调用只会在事务提交后发生。"""
    if connection_id is None:
        raise CalendarWriteError("日历连接不可用")
    result = await session.execute(
        text(
            "INSERT INTO external_action_executions "
            "(tenant_id, approval_item_id, connection_id, idempotency_key, status, request_summary, started_at) "
            "VALUES (:tenant_id, :approval_item_id, :connection_id, :idempotency_key, 'running', CAST(:request AS jsonb), now()) "
            "RETURNING id"
        ),
        {"tenant_id": run.tenant_id, "approval_item_id": approval_item_id, "connection_id": connection_id, "idempotency_key": request_payload["idempotency_key"], "request": json.dumps(request_payload, ensure_ascii=False)},
    )
    return result.mappings().one()["id"]
