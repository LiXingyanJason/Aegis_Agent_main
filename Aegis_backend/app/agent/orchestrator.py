"""最小 Agent 执行编排：读取会话、调用 LLM、保存回复。"""

import asyncio
import json
import logging
from dataclasses import dataclass
from time import monotonic
from uuid import UUID

from app.agent.tool_router import AgentToolRouter
from app.config.database import Database, TenantContext, tenant_transaction
from app.event.run_event import RunEventPublisher
from app.llm.client import LLMError, LLMMessage, OpenAICompatibleClient
from app.repository.run_repository import AgentRunRepository
from app.repository.tool_repository import ToolCallRepository
from app.tool.contracts import ToolError, ToolInvocation
from app.tool.tool_gateway import ToolGateway

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    """Worker 输出所需的任务执行结果；错误信息已在业务层脱敏。"""

    status: str
    error_code: str | None = None
    error_message: str | None = None


class AgentOrchestrator:
    """消费一个 queued 任务并完成第一版文本回复。"""

    _system_prompt = "你是 Aegis PA 个人效率助手。请基于当前会话，给出准确、简洁、可执行的中文回复。"

    def __init__(
        self,
        database: Database,
        llm_client: OpenAICompatibleClient,
        runs: AgentRunRepository | None = None,
        events: RunEventPublisher | None = None,
        tools: ToolGateway | None = None,
        tool_calls: ToolCallRepository | None = None,
        tool_router: AgentToolRouter | None = None,
    ) -> None:
        self._database = database
        self._llm_client = llm_client
        self._runs = runs or AgentRunRepository()
        self._events = events
        self._tools = tools
        self._tool_calls = tool_calls or ToolCallRepository()
        self._tool_router = tool_router or AgentToolRouter()

    async def execute(self, run_id: UUID) -> AgentExecutionResult:
        """执行任务，并返回 `completed`、`failed` 或 `ignored` 供 Worker 输出运行状态。"""
        started_event = None
        async with self._database.session() as session:
            async with session.begin():
                run = await self._runs.claim_queued_run(
                    session,
                    run_id,
                    model_provider=self._llm_client.provider_name,
                    model_name=self._llm_client.model_name,
                )
                if run is not None and self._events is not None:
                    started_event = await self._runs.append_event(
                        session,
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        event_type="run_started",
                        payload={
                            "status": "running",
                            "current_stage": "正在调用模型",
                            "model_provider": self._llm_client.provider_name,
                            "model_name": self._llm_client.model_name,
                        },
                    )
        if run is None:
            return AgentExecutionResult(status="ignored")
        if started_event is not None:
            await self._events.publish_best_effort(started_event)

        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        step_id: UUID | None = None
        try:
            async with self._database.session() as session:
                async with tenant_transaction(session, context):
                    history = await self._runs.load_conversation_history(
                        session,
                        conversation_id=run.conversation_id,
                        tenant_id=run.tenant_id,
                        user_id=run.user_id,
                    )

            tool_context = await self._execute_selected_tool(run, context, history)

            async with self._database.session() as session:
                async with tenant_transaction(session, context):
                    step_id = await self._runs.create_running_llm_step(
                        session, run_id=run.id, tenant_id=run.tenant_id
                    )
                    if self._events is not None:
                        event = await self._runs.append_event(
                            session,
                            run_id=run.id,
                            tenant_id=run.tenant_id,
                            event_type="progress_updated",
                            payload={
                                "step_id": str(step_id),
                                "label": "正在生成回复",
                                "status": "running",
                            },
                        )
                        self._events.publish_after_commit(event)

            messages = [LLMMessage(role="system", content=self._system_prompt)]
            if tool_context is not None:
                messages.append(LLMMessage(role="system", content=tool_context))
            messages.extend(LLMMessage(role=item["role"], content=item["content"]) for item in history)
            reply = await self._llm_client.complete(messages)

            async with self._database.session() as session:
                async with tenant_transaction(session, context):
                    await self._runs.complete_run_with_assistant_message(
                        session,
                        run_id=run.id,
                        conversation_id=run.conversation_id,
                        tenant_id=run.tenant_id,
                        user_id=run.user_id,
                        assistant_content=reply,
                        step_id=step_id,
                    )
                    if self._events is not None:
                        message_event = await self._runs.append_event(
                            session,
                            run_id=run.id,
                            tenant_id=run.tenant_id,
                            event_type="assistant_message_completed",
                            payload={"content": reply, "is_final": True},
                        )
                        completed_event = await self._runs.append_event(
                            session,
                            run_id=run.id,
                            tenant_id=run.tenant_id,
                            event_type="run_completed",
                            payload={"status": "completed", "result_summary": reply[:1000]},
                        )
                        self._events.publish_after_commit(message_event)
                        self._events.publish_after_commit(completed_event)
            return AgentExecutionResult(status="completed")
        except asyncio.CancelledError:
            raise
        except LLMError as error:
            await self._mark_failed(run, step_id, "LLM_UNAVAILABLE", str(error))
            return AgentExecutionResult(
                status="failed", error_code="LLM_UNAVAILABLE", error_message=str(error)
            )
        except Exception:
            # 仅写入 Worker 本地日志以帮助开发排错；数据库和接口仍使用脱敏错误文本。
            logger.exception("Agent 执行失败：run_id=%s", run.id)
            await self._mark_failed(run, step_id, "AGENT_EXECUTION_FAILED", "任务执行失败")
            return AgentExecutionResult(
                status="failed", error_code="AGENT_EXECUTION_FAILED", error_message="任务执行失败"
            )

    async def _execute_selected_tool(self, run, context: TenantContext, history) -> str | None:
        """选择并执行一期只读日历工具；工具故障转为可见结果，不中断普通 LLM 回复。"""
        if self._tools is None:
            return None
        latest_user_message = next(
            (item["content"] for item in reversed(history) if item["role"] == "user"), None
        )
        if not isinstance(latest_user_message, str):
            return None
        invocation = self._tool_router.select(latest_user_message)
        if invocation is None:
            return None
        return await self._invoke_read_tool(run, context, invocation)

    async def _invoke_read_tool(
        self, run, context: TenantContext, invocation: ToolInvocation
    ) -> str:
        """持久化只读工具调用、发出 SSE 预览事件，并返回可供 LLM 使用的可信上下文。"""
        assert self._tools is not None
        definition = self._tools.get_definition(invocation.tool_name)
        label = "正在查询可用时间" if invocation.tool_name.endswith("find_free_time") else "正在查询日程"
        step_key = invocation.tool_name.replace(".", "_")
        step_id: UUID | None = None
        tool_call_id: UUID | None = None
        prepared = None
        started_at = monotonic()
        try:
            async with self._database.session() as session:
                async with tenant_transaction(session, context):
                    step_id = await self._runs.create_running_tool_step(
                        session,
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        step_key=step_key,
                        label=label,
                    )
                    try:
                        prepared = await self._tools.prepare(
                            session,
                            tenant_id=run.tenant_id,
                            user_id=run.user_id,
                            run_id=run.id,
                            invocation=invocation,
                        )
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
            result = await self._tools.invoke(prepared)
        except ToolError as error:
            await self._finish_tool_failure(
                run, context, step_id, tool_call_id, definition, error, int((monotonic() - started_at) * 1000)
            )
            return f"日历工具调用失败：{error.message}。请说明无法取得日历结果，不要编造信息。"

        duration_ms = int((monotonic() - started_at) * 1000)
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
        self, run, context: TenantContext, step_id, tool_call_id, definition, error: ToolError, duration_ms: int
    ) -> None:
        """记录远程 Calendar MCP 调用失败，并通过 SSE 显示只读工具错误。"""
        if step_id is None or tool_call_id is None:
            return
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
        self, session, *, run, step_id, tool_call_id, definition, error: ToolError
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

    async def _mark_failed(
        self, run, step_id: UUID | None, error_code: str, error_message: str
    ) -> None:
        """在可确定的租户上下文中记录执行失败，避免 Worker 直接向外抛出业务异常。"""
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                await self._runs.fail_run(
                    session,
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    step_id=step_id,
                    error_code=error_code,
                    error_message=error_message,
                )
                if self._events is not None:
                    event = await self._runs.append_event(
                        session,
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        event_type="run_failed",
                        payload={
                            "status": "failed",
                            "error_code": error_code,
                            "error_message": error_message,
                        },
                    )
                    self._events.publish_after_commit(event)


def _input_summary(arguments: dict) -> dict:
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
