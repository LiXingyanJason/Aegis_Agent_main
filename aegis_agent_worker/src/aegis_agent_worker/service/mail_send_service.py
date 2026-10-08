"""已批准邮件的受控发送用例。"""

import json
from time import monotonic
from uuid import UUID

from sqlalchemy import text

from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.event.run_event import RunEventPublisher
from aegis_agent_worker.repository.run_repository import AgentRunRepository, ClaimedRun
from aegis_agent_worker.repository.tool_repository import ToolCallRepository
from aegis_agent_worker.tool.contracts import ToolContext, ToolError
from aegis_agent_worker.tool.email.mcp_client import EmailMCPClient


class MailSendError(RuntimeError):
    """批准后的邮件发送未成功完成时抛出的受控异常。"""


class MailSendService:
    """将确认决定转换为一次可审计、可幂等的 Email MCP 写操作。"""

    def __init__(
        self,
        database: Database,
        runs: AgentRunRepository,
        email_client: EmailMCPClient,
        events: RunEventPublisher | None = None,
        tool_calls: ToolCallRepository | None = None,
    ) -> None:
        self._database = database
        self._runs = runs
        self._email_client = email_client
        self._events = events
        self._tool_calls = tool_calls or ToolCallRepository()

    async def execute(self, run: ClaimedRun, decision: str) -> None:
        """拒绝时恢复草稿，批准时发送；二者均结束当前独立 mail_send 任务。"""
        if decision == "rejected":
            await self._complete_rejected(run)
            return
        if decision != "approved":
            raise MailSendError("无效的邮件发送确认决定")

        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        started_at = monotonic()
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                payload = await _load_approved_draft(session, run)
                if payload is None:
                    raise MailSendError("邮件发送确认已失效或草稿版本已变化")
                step_id = await self._runs.create_running_tool_step(
                    session, run_id=run.id, tenant_id=run.tenant_id,
                    step_key="mail_messages_send", label="正在发送邮件",
                )
                request_payload = {
                    "to": payload["to"], "cc": payload["cc"], "subject": payload["subject"],
                    "body": payload["body"], "idempotency_key": f"mail:{payload['approval_item_id']}:1",
                }
                execution_id = await _create_execution(session, run, payload, request_payload)
                tool_call_id = await self._tool_calls.create_running(
                    session, tenant_id=run.tenant_id, run_id=run.id, step_id=step_id,
                    connection_id=payload["connection_id"], tool_name="mail.messages.send",
                    risk_level="sensitive", tool_version="1.0", mcp_server="aegis-email-mcp",
                    request_payload=request_payload,
                    input_summary={"to": payload["to"], "cc": payload["cc"], "subject": payload["subject"]},
                )
                if self._events is not None:
                    event = await self._runs.append_event(
                        session, run_id=run.id, tenant_id=run.tenant_id, event_type="progress_updated",
                        payload={"step_id": str(step_id), "label": "正在发送邮件", "status": "running"},
                    )
                    self._events.publish_after_commit(event)

        try:
            receipt = await self._email_client.send_message(
                ToolContext(run.tenant_id, run.user_id, run.id, payload["connection_id"]),
                to=payload["to"], cc=payload["cc"], subject=payload["subject"], body=payload["body"],
                idempotency_key=request_payload["idempotency_key"],
            )
        except ToolError as error:
            await self._mark_failed(run, payload, step_id, tool_call_id, execution_id, error)
            raise MailSendError(error.message) from error

        provider_message_id = receipt.get("provider_message_id")
        if not isinstance(provider_message_id, str) or not provider_message_id:
            error = ToolError("EMAIL_TOOL_INVALID_RESPONSE", "邮件工具未返回发送后的外部邮件标识")
            await self._mark_failed(run, payload, step_id, tool_call_id, execution_id, error)
            raise MailSendError(error.message)
        duration_ms = int((monotonic() - started_at) * 1000)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                summary = {"provider_message_id": provider_message_id, "recipients": receipt.get("recipients", {})}
                await self._tool_calls.succeed(session, tool_call_id=tool_call_id, tenant_id=run.tenant_id,
                                               response_payload=receipt, output_summary=summary, duration_ms=duration_ms)
                await self._runs.finish_step(session, step_id=step_id, run_id=run.id, tenant_id=run.tenant_id,
                                             status="succeeded", detail="邮件已发送")
                await session.execute(text("UPDATE external_action_executions SET status='succeeded', provider_resource_id=:provider_message_id, result_summary=CAST(:result AS jsonb), finished_at=now() WHERE id=:execution_id"),
                                      {"execution_id": execution_id, "provider_message_id": provider_message_id, "result": json.dumps(summary, ensure_ascii=False)})
                await session.execute(text("UPDATE mail_drafts SET status='sent', provider_message_id=:provider_message_id, sent_at=now(), updated_at=now() WHERE id=:draft_id"),
                                      {"draft_id": payload["draft_id"], "provider_message_id": provider_message_id})
                await session.execute(text("INSERT INTO sent_mail_messages (tenant_id,user_id,draft_id,execution_id,connection_id,provider_message_id,subject,recipient_summary,sent_at,status) VALUES (:tenant_id,:user_id,:draft_id,:execution_id,:connection_id,:provider_message_id,:subject,CAST(:recipients AS jsonb),now(),'sent') ON CONFLICT (draft_id) DO NOTHING"),
                                      {"tenant_id": run.tenant_id, "user_id": run.user_id, "draft_id": payload["draft_id"], "execution_id": execution_id, "connection_id": payload["connection_id"], "provider_message_id": provider_message_id, "subject": payload["subject"], "recipients": json.dumps({"to": payload["to"], "cc": payload["cc"]}, ensure_ascii=False)})
                await session.execute(text("UPDATE approval_items SET status='executed', updated_at=now() WHERE id=:approval_item_id"), {"approval_item_id": payload["approval_item_id"]})
                await _complete_run(session, run, "邮件已发送")
                if self._events is not None:
                    for event_type, event_payload in (
                        ("tool_preview", {"tool_call_id": str(tool_call_id), "tool_name": "mail.messages.send", "risk_level": "sensitive", "output_summary": summary, "status": "succeeded"}),
                        ("approval_executed", {"approval_item_id": str(payload["approval_item_id"]), "resource_type": "mail_draft", "resource_id": str(payload["draft_id"]), "provider_resource_id": provider_message_id, "status": "executed"}),
                        ("run_completed", {"status": "completed", "result_summary": "邮件已发送"}),
                    ):
                        event = await self._runs.append_event(session, run_id=run.id, tenant_id=run.tenant_id, event_type=event_type, payload=event_payload)
                        self._events.publish_after_commit(event)

    async def _complete_rejected(self, run: ClaimedRun) -> None:
        """拒绝发送不产生外部副作用，并将草稿退回可编辑状态。"""
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                result = await session.execute(text("SELECT resource_id FROM approval_items ai JOIN approval_decisions ad ON ad.approval_item_id=ai.id WHERE ai.run_id=:run_id AND ai.tenant_id=:tenant_id AND ad.decision='rejected' FOR UPDATE"), {"run_id": run.id, "tenant_id": run.tenant_id})
                row = result.mappings().first()
                if row is None:
                    raise MailSendError("未找到被拒绝的邮件发送确认项")
                await session.execute(text("UPDATE mail_drafts SET status='draft', updated_at=now() WHERE id=:draft_id AND tenant_id=:tenant_id"), {"draft_id": row["resource_id"], "tenant_id": run.tenant_id})
                await _complete_run(session, run, "已拒绝发送邮件")
                if self._events is not None:
                    event = await self._runs.append_event(session, run_id=run.id, tenant_id=run.tenant_id, event_type="run_completed", payload={"status": "completed", "result_summary": "已拒绝发送邮件"})
                    self._events.publish_after_commit(event)

    async def _mark_failed(self, run, payload, step_id, tool_call_id, execution_id, error: ToolError) -> None:
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                await self._tool_calls.fail(session, tool_call_id=tool_call_id, tenant_id=run.tenant_id, error_code=error.code, error_message=error.message, duration_ms=0)
                await self._runs.finish_step(session, step_id=step_id, run_id=run.id, tenant_id=run.tenant_id, status="failed", detail=error.message)
                await session.execute(text("UPDATE external_action_executions SET status='failed', error_code=:code, error_message=:message, finished_at=now() WHERE id=:execution_id"), {"execution_id": execution_id, "code": error.code, "message": error.message[:1000]})
                await session.execute(text("UPDATE mail_drafts SET status='failed', updated_at=now() WHERE id=:draft_id"), {"draft_id": payload["draft_id"]})
                await session.execute(text("UPDATE approval_items SET status='failed', updated_at=now() WHERE id=:approval_item_id"), {"approval_item_id": payload["approval_item_id"]})


async def _load_approved_draft(session, run: ClaimedRun):
    result = await session.execute(text("SELECT d.id AS draft_id,d.subject,d.body,d.send_connection_id AS connection_id,ai.id AS approval_item_id FROM mail_drafts d JOIN approval_items ai ON ai.resource_id=d.id JOIN approval_decisions ad ON ad.approval_item_id=ai.id JOIN provider_connections pc ON pc.id=d.send_connection_id WHERE ai.run_id=:run_id AND d.tenant_id=:tenant_id AND d.user_id=:user_id AND d.status='pending_approval' AND ai.status='approved_executing' AND ai.draft_version=d.version AND ad.decision='approved' AND pc.status='active' AND (pc.expires_at IS NULL OR pc.expires_at>now()) AND pc.scopes @> '[\"mail.send\"]'::jsonb FOR UPDATE OF d,ai"), {"run_id": run.id, "tenant_id": run.tenant_id, "user_id": run.user_id})
    payload = result.mappings().first()
    if payload is None:
        return None
    recipients = await session.execute(text("SELECT recipient_type,email FROM mail_draft_recipients WHERE draft_id=:draft_id AND tenant_id=:tenant_id ORDER BY email"), {"draft_id": payload["draft_id"], "tenant_id": run.tenant_id})
    grouped = {"to": [], "cc": []}
    for recipient in recipients.mappings():
        if recipient["recipient_type"] in grouped:
            grouped[recipient["recipient_type"]].append(recipient["email"])
    return {**dict(payload), **grouped}


async def _create_execution(session, run: ClaimedRun, payload, request_payload: dict) -> UUID:
    result = await session.execute(text("INSERT INTO external_action_executions (tenant_id,approval_item_id,connection_id,idempotency_key,status,request_summary,started_at) VALUES (:tenant_id,:approval_item_id,:connection_id,:idempotency_key,'running',CAST(:request AS jsonb),now()) RETURNING id"), {"tenant_id": run.tenant_id, "approval_item_id": payload["approval_item_id"], "connection_id": payload["connection_id"], "idempotency_key": request_payload["idempotency_key"], "request": json.dumps(request_payload, ensure_ascii=False)})
    return result.mappings().one()["id"]


async def _complete_run(session, run: ClaimedRun, summary: str) -> None:
    await session.execute(text("UPDATE agent_runs SET status='completed', current_stage='已完成', result_summary=:summary, finished_at=now(), updated_at=now() WHERE id=:run_id AND tenant_id=:tenant_id"), {"run_id": run.id, "tenant_id": run.tenant_id, "summary": summary})
