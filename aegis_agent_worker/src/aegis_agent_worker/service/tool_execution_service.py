"""受控工具调用的事务、审计与 SSE 预览编排。"""

import json
import logging
from time import monotonic
from typing import Any
from uuid import UUID

from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.event.run_event import RunEventPublisher
from aegis_agent_worker.observability.structured_logging import log_event
from aegis_agent_worker.observability.telemetry import get_tracer
from aegis_agent_worker.repository.run_repository import AgentRunRepository, ClaimedRun
from aegis_agent_worker.repository.tool_repository import ToolCallRepository
from aegis_agent_worker.tool.contracts import ToolError, ToolInvocation
from aegis_agent_worker.tool.gateway import ToolGateway

logger = logging.getLogger(__name__)


class ToolExecutionService:
    """将工具网络调用与其步骤、审计、事件记录组织为可复用应用服务。"""

    def __init__(
        self,
        database: Database,
        runs: AgentRunRepository,
        tools: ToolGateway,
        tool_calls: ToolCallRepository,
        events: RunEventPublisher | None = None,
    ) -> None:
        self._database = database
        self._runs = runs
        self._tools = tools
        self._tool_calls = tool_calls
        self._events = events

    async def invoke_read_tool(
        self, run: ClaimedRun, context: TenantContext, invocation: ToolInvocation
    ) -> str:
        """
        持久化只读工具调用、发出 SSE 预览事件，并返回可供 LLM 使用的可信上下文。
        invocation:需要调用工具的名称
        """
        definition = self._tools.get_definition(invocation.tool_name) # 读取该工具权威定义(写入数据库),工具白名单检查
        label = "正在查询可用时间" if invocation.tool_name.endswith("find_free_time") else "正在查询日程"
        step_key = invocation.tool_name.replace(".", "_")
        step_id: UUID | None = None
        tool_call_id: UUID | None = None
        prepared = None
        started_at = monotonic() # 记录本次工具调用的单调时钟起点
        try:
            async with self._database.session() as session:
                async with tenant_transaction(session, context):
                    #  run_steps 表创建当前任务的一个工具步骤 status: running
                    step_id = await self._runs.create_running_tool_step(
                        session,
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        step_key=step_key,
                        label=label,
                    )
                    try:
                        # ToolGateway.prepare() 做调用前校验和准备
                        prepared = await self._tools.prepare(
                            session,
                            tenant_id=run.tenant_id,
                            user_id=run.user_id,
                            run_id=run.id,
                            invocation=invocation,
                        )

                    # ToolGateway校验阶段失败，记录“用户曾尝试调用哪个工具、传了什么安全参数、为什么失败”，用于追溯
                    except ToolError as error:
                        tool_call_id = await self._tool_calls.create_running(
                            session,
                            tenant_id=run.tenant_id,
                            run_id=run.id,
                            step_id=step_id,
                            connection_id=None,
                            tool_name=definition.name,
                            risk_level=definition.risk_level,
                            tool_version=definition.version,
                            mcp_server=definition.mcp_server,
                            request_payload=invocation.arguments,
                            input_summary=_input_summary(invocation.arguments),
                        )
                        await self._tool_calls.fail(
                            session,
                            tool_call_id=tool_call_id,
                            tenant_id=run.tenant_id,
                            error_code=error.code,
                            error_message=error.message,
                            duration_ms=0,
                        )
                        await self._runs.finish_step(
                            session,
                            step_id=step_id,
                            run_id=run.id,
                            tenant_id=run.tenant_id,
                            status="failed",
                            detail=error.message,
                        )
                        await self._append_tool_failure_events(
                            session,
                            run=run,
                            step_id=step_id,
                            tool_call_id=tool_call_id,
                            definition=definition,
                            error=error,
                        )
                        return f"日历工具暂不可用：{error.message}。请明确告知用户，不要编造日程或空闲时间。"

                    # 工具准备成功后，登记一次真正即将发生的工具调用，并通知前端开始执行
                    # 在 tool_calls 表新增一条记录，状态为 running
                    tool_call_id = await self._tool_calls.create_running(
                        session,
                        tenant_id=run.tenant_id,
                        run_id=run.id,
                        step_id=step_id,
                        connection_id=prepared.context.connection_id,
                        tool_name=definition.name,
                        risk_level=definition.risk_level,
                        tool_version=definition.version,
                        mcp_server=definition.mcp_server,
                        request_payload=invocation.arguments,
                        input_summary=_input_summary(invocation.arguments),
                    )
                    # Worker 启用了 SSE 事件发布器，就写入一条任务进度事件 progress_updated
                    if self._events is not None:
                        event = await self._runs.append_event(
                            session,
                            run_id=run.id,
                            tenant_id=run.tenant_id,
                            event_type="progress_updated",
                            payload={"step_id": str(step_id), "label": label, "status": "running"},
                        )
                        self._events.publish_after_commit(event)

            assert prepared is not None and step_id is not None and tool_call_id is not None
            # 调用工具，返回结果
            tracer = get_tracer(__name__)
            with tracer.start_as_current_span("tool.invoke") as span:
                span.set_attribute("aegis.run_id", str(run.id))
                span.set_attribute("aegis.tool_call_id", str(tool_call_id))
                span.set_attribute("aegis.tool.name", definition.name)
                result = await self._tools.invoke(prepared)
        except ToolError as error:
            await self._finish_tool_failure(
                run, context, step_id, tool_call_id, definition, error, int((monotonic() - started_at) * 1000)
            )
            return f"日历工具调用失败：{error.message}。请说明无法取得日历结果，不要编造信息。"

        duration_ms = int((monotonic() - started_at) * 1000)
        log_event(
            logger,
            logging.INFO,
            "tool_call_completed",
            run_id=run.id,
            tool_call_id=tool_call_id,
            tool_name=definition.name,
            status="succeeded",
            duration_ms=duration_ms,
        )
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                await self._tool_calls.succeed(
                    session,
                    tool_call_id=tool_call_id,
                    tenant_id=run.tenant_id,
                    response_payload=result.response_payload,
                    output_summary=result.output_summary,
                    duration_ms=duration_ms,
                )
                await self._runs.finish_step(
                    session,
                    step_id=step_id,
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    status="succeeded",
                    detail="日历查询完成",
                )
                if self._events is not None:
                    preview_event = await self._runs.append_event(
                        session,
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        event_type="tool_preview",
                        payload={
                            "tool_call_id": str(tool_call_id),
                            "tool_name": definition.name,
                            "risk_level": definition.risk_level,
                            "input_summary": _input_summary(invocation.arguments),
                            "output_summary": result.output_summary,
                            "status": "succeeded",
                        },
                    )
                    progress_event = await self._runs.append_event(
                        session,
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        event_type="progress_updated",
                        payload={
                            "step_id": str(step_id),
                            "label": label,
                            "status": "succeeded",
                            "detail": "日历查询完成",
                        },
                    )
                    self._events.publish_after_commit(preview_event)
                    self._events.publish_after_commit(progress_event)
        return (
            "以下是 Calendar MCP 工具返回的可信只读结果。只能基于这些结果回答。"
            "若调用参数含 requested_date，表示系统已按 timezone 将相对日期（如“明天”）"
            "解析为该具体日期；应直接回答，不能要求用户再次确认今天日期或时区。"
            "若工具结果包含 start_at_local/end_at_local，优先使用其本地时间；"
            "参会人字段是当前用户授权的日历查询结果，可如实列出，不要自行改写为“已隐去”。\n"
            + json.dumps(
                {"tool_arguments": invocation.arguments, "tool_result": result.output_summary},
                ensure_ascii=False,
            )
        )

    async def _finish_tool_failure(
        self,
        run: ClaimedRun,
        context: TenantContext,
        step_id: UUID | None,
        tool_call_id: UUID | None,
        definition: Any,
        error: ToolError,
        duration_ms: int,
    ) -> None:
        """记录远程 Calendar MCP 调用失败，并通过 SSE 显示只读工具错误。"""
        if step_id is None or tool_call_id is None:
            return
        log_event(
            logger,
            logging.ERROR,
            "tool_call_failed",
            run_id=run.id,
            tool_call_id=tool_call_id,
            tool_name=definition.name,
            status="failed",
            error_code=error.code,
            duration_ms=duration_ms,
        )
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                await self._tool_calls.fail(
                    session,
                    tool_call_id=tool_call_id,
                    tenant_id=run.tenant_id,
                    error_code=error.code,
                    error_message=error.message,
                    duration_ms=duration_ms,
                )
                await self._runs.finish_step(
                    session,
                    step_id=step_id,
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    status="failed",
                    detail=error.message,
                )
                await self._append_tool_failure_events(
                    session,
                    run=run,
                    step_id=step_id,
                    tool_call_id=tool_call_id,
                    definition=definition,
                    error=error,
                )

    async def _append_tool_failure_events(
        self,
        session: Any,
        *,
        run: ClaimedRun,
        step_id: UUID,
        tool_call_id: UUID,
        definition: Any,
        error: ToolError,
    ) -> None:
        """在同一事务中记录工具失败预览及需要连接提示，并在提交后推送。"""
        if self._events is None:
            return
        preview_event = await self._runs.append_event(
            session,
            run_id=run.id,
            tenant_id=run.tenant_id,
            event_type="tool_preview",
            payload={
                "tool_call_id": str(tool_call_id),
                "tool_name": definition.name,
                "risk_level": definition.risk_level,
                "status": "failed",
                "error_code": error.code,
                "error_message": error.message,
            },
        )
        progress_event = await self._runs.append_event(
            session,
            run_id=run.id,
            tenant_id=run.tenant_id,
            event_type="progress_updated",
            payload={
                "step_id": str(step_id),
                "label": "日历查询不可用",
                "status": "failed",
                "detail": error.message,
            },
        )
        self._events.publish_after_commit(preview_event)
        self._events.publish_after_commit(progress_event)
        if error.code == "CALENDAR_CONNECTION_REQUIRED":
            connection_event = await self._runs.append_event(
                session,
                run_id=run.id,
                tenant_id=run.tenant_id,
                event_type="connection_required",
                payload={
                    "provider": "calendar",
                    "required_scopes": ["calendar.read"],
                    "message": error.message,
                },
            )
            self._events.publish_after_commit(connection_event)


def _input_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    """生成可展示的只读调用摘要，不额外写入用户身份或连接凭据。"""
    summary = {
        key: value
        for key, value in arguments.items()
        if key in {"start_at", "end_at", "duration_minutes", "participants", "requested_date", "timezone"}
    }
    participants = summary.get("participants")
    if isinstance(participants, list):
        summary["participant_count"] = len(participants)
        summary.pop("participants")
    return summary
