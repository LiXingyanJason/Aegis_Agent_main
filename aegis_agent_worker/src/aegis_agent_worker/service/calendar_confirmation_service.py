"""会议安排草稿与逐项确认的应用服务。"""

import json
from datetime import datetime, timedelta
from hashlib import sha256
from time import monotonic
from typing import Any
from uuid import UUID

from sqlalchemy import text

from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.event.run_event import RunEventPublisher
from aegis_agent_worker.repository.run_repository import AgentRunRepository, ClaimedRun
from aegis_agent_worker.repository.tool_repository import ToolCallRepository
from aegis_agent_worker.service.tool_execution_service import ToolExecutionService
from aegis_agent_worker.tool.contracts import ToolError, ToolInvocation
from aegis_agent_worker.tool.gateway import ToolGateway


class CalendarWriteError(RuntimeError):
    """已批准日历写入未完成时抛出的受控错误。"""


class CalendarConfirmationService:
    """将可用时间查询结果转换为持久化会议草稿和待确认操作。

    此服务只创建草稿，绝不调用 ``calendar.create_event``。外部写入必须等批准后
    由恢复工作流执行。
    """

    def __init__(
        self,
        database: Database,
        runs: AgentRunRepository,
        tool_execution: ToolExecutionService | None,
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

    async def prepare(
        self,
        run: ClaimedRun,
        *,
        invocation: ToolInvocation,
        title: str,
        attendees: list[str],
    ) -> dict[str, Any]:
        """查询空闲时间并创建一个待确认的会议草稿。"""
        if self._tool_execution is None:
            return {"confirmation_error": "当前日历工具尚未配置，无法生成会议草稿。"}

        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        tool_context = await self._tool_execution.invoke_read_tool(run, context, invocation)
        free_slot = _first_free_slot(tool_context)
        if free_slot is None:
            return {"confirmation_error": "未查询到可用于安排会议的空闲时间，未创建草稿。"}

        start_at, end_at = free_slot
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                connection_id = await _find_calendar_connection(session, run.tenant_id, run.user_id)
                if connection_id is None:
                    return {"confirmation_error": "当前用户没有有效日历连接，未创建草稿。"}
                draft_id = await _create_draft(
                    session,
                    run=run,
                    connection_id=connection_id,
                    title=title,
                    start_at=start_at,
                    end_at=end_at,
                    attendees=attendees,
                )
                approval_item_id = await _create_approval_item(
                    session,
                    run=run,
                    draft_id=draft_id,
                    title=title,
                    start_at=start_at,
                    end_at=end_at,
                    attendees=attendees,
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
                            "title": title,
                            "action": "calendar.create_event",
                            "status": "pending",
                            "preview": {
                                "start_at": start_at.isoformat(),
                                "end_at": end_at.isoformat(),
                                "attendees": attendees,
                            },
                        },
                    )
                    self._events.publish_after_commit(event)
        return {
            "awaiting_confirmation": True,
            "draft_id": str(draft_id),
            "approval_item_id": str(approval_item_id),
            "assistant_content": "我已根据空闲时间生成会议草稿，请在确认页逐项确认后再创建日历事件。",
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
                draft = await _load_approved_draft(
                    session, run=run, draft_id=draft_id, approval_item_id=approval_item_id
                )
                if draft is None:
                    raise CalendarWriteError("会议草稿未获批准、已失效或不属于当前任务")
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
            raise CalendarWriteError(error.message) from error

        duration_ms = int((monotonic() - started_at) * 1000)
        external_event_id = str(result.response_payload.get("external_event_id", ""))
        if not external_event_id:
            await self._mark_write_failed(
                run, context, approval_item_id, execution_id, step_id, tool_call_id,
                ToolError("TOOL_INVALID_RESPONSE", "日历服务未返回外部事件标识"),
            )
            raise CalendarWriteError("日历服务未返回外部事件标识")
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


def _first_free_slot(tool_context: str) -> tuple[datetime, datetime] | None:
    """从只读工具的可信摘要中取第一个空闲时段；失败文本不会被当作日历数据。"""
    try:
        payload = json.loads(tool_context.rsplit("\n", 1)[-1])
        slots = payload["tool_result"]["available_slots"]
        first_slot = slots[0]
        return datetime.fromisoformat(first_slot["start_at"]), datetime.fromisoformat(first_slot["end_at"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


async def _find_calendar_connection(session, tenant_id: UUID, user_id: UUID) -> UUID | None:
    result = await session.execute(
        text(
            "SELECT id FROM provider_connections WHERE tenant_id = :tenant_id "
            "AND user_id = :user_id AND provider IN ('google_calendar', 'outlook_calendar') "
            "AND status = 'active' ORDER BY last_verified_at DESC NULLS LAST, created_at DESC LIMIT 1"
        ),
        {"tenant_id": tenant_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return row["id"] if row else None


async def _create_draft(session, *, run: ClaimedRun, connection_id: UUID, title: str, start_at: datetime, end_at: datetime, attendees: list[str]) -> UUID:
    result = await session.execute(
        text(
            "INSERT INTO calendar_event_drafts "
            "(tenant_id, user_id, run_id, connection_id, calendar_external_id, calendar_name, title, description, start_at, end_at, timezone, status) "
            "VALUES (:tenant_id, :user_id, :run_id, :connection_id, 'primary', '主日历', :title, "
            ":description, :start_at, :end_at, :timezone, 'pending_approval') RETURNING id"
        ),
        {"tenant_id": run.tenant_id, "user_id": run.user_id, "run_id": run.id, "connection_id": connection_id, "title": title[:500], "description": "由 Aegis PA 生成，等待用户确认。", "start_at": start_at, "end_at": end_at, "timezone": "Asia/Shanghai"},
    )
    draft_id = result.mappings().one()["id"]
    for email in sorted(set(attendees)):
        await session.execute(
            text(
                "INSERT INTO calendar_draft_attendees (tenant_id, draft_id, email) "
                "VALUES (:tenant_id, :draft_id, :email) ON CONFLICT (draft_id, email) DO NOTHING"
            ),
            {"tenant_id": run.tenant_id, "draft_id": draft_id, "email": email},
        )
    return draft_id


async def _create_approval_item(session, *, run: ClaimedRun, draft_id: UUID, title: str, start_at: datetime, end_at: datetime, attendees: list[str]) -> UUID:
    preview = {"title": title, "start_at": start_at.isoformat(), "end_at": end_at.isoformat(), "attendees": attendees}
    parameters_hash = sha256(json.dumps(preview, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    result = await session.execute(
        text(
            "INSERT INTO approval_items "
            "(tenant_id, user_id, run_id, action, risk_level, resource_type, resource_id, title, preview_snapshot, parameters_hash, draft_version, expires_at) "
            "VALUES (:tenant_id, :user_id, :run_id, 'calendar.create_event', 'write', "
            "'calendar_event_draft', :draft_id, :title, CAST(:preview AS jsonb), :parameters_hash, 1, now() + interval '24 hours') RETURNING id"
        ),
        {"tenant_id": run.tenant_id, "user_id": run.user_id, "run_id": run.id, "draft_id": draft_id, "title": f"创建日历事件：{title}"[:256], "preview": json.dumps(preview, ensure_ascii=False), "parameters_hash": parameters_hash},
    )
    return result.mappings().one()["id"]


async def _load_approved_draft(session, *, run: ClaimedRun, draft_id: UUID, approval_item_id: UUID):
    """锁定草稿与确认项，并确认批准决定与当前任务、租户完全一致。"""
    result = await session.execute(
        text(
            "SELECT d.title, d.description, d.start_at, d.end_at, d.timezone "
            "FROM calendar_event_drafts d JOIN approval_items ai ON ai.resource_id = d.id "
            "JOIN approval_decisions ad ON ad.approval_item_id = ai.id "
            "WHERE d.id = :draft_id AND d.run_id = :run_id AND d.tenant_id = :tenant_id "
            "AND ai.id = :approval_item_id AND ai.run_id = :run_id "
            "AND ai.status = 'approved_executing' AND ad.decision = 'approved' "
            "FOR UPDATE OF d, ai"
        ),
        {"draft_id": draft_id, "approval_item_id": approval_item_id, "run_id": run.id, "tenant_id": run.tenant_id},
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
