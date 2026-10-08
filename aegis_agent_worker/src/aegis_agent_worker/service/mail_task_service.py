"""邮件摘要与回复草稿的 Worker 应用服务。"""

import json
from hashlib import sha256
from typing import Any

from sqlalchemy import text

from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.event.run_event import RunEventPublisher
from aegis_agent_worker.llm.client import LLMMessage, OpenAICompatibleClient
from aegis_agent_worker.repository.mail_repository import MailTaskRepository
from aegis_agent_worker.repository.run_repository import AgentRunRepository, ClaimedRun
from aegis_agent_worker.tool.contracts import ToolContext
from aegis_agent_worker.tool.email.mcp_client import EmailMCPClient


class MailTaskError(RuntimeError):
    """邮件正文、模型输出或任务上下文不符合预期时抛出。"""


class MailTaskService:
    """执行 mail_extraction / mail_reply_draft 两种非对话后台任务。"""

    def __init__(
        self,
        database: Database,
        runs: AgentRunRepository,
        llm_client: OpenAICompatibleClient,
        email_client: EmailMCPClient,
        events: RunEventPublisher | None = None,
        repository: MailTaskRepository | None = None,
    ) -> None:
        self._database = database
        self._runs = runs
        self._llm_client = llm_client
        self._email_client = email_client
        self._events = events
        self._repository = repository or MailTaskRepository()

    async def execute(self, run: ClaimedRun) -> None:
        """按任务类型读取邮件、调用模型并保存相应的站内派生结果。"""
        if run.run_type == "mail_extraction":
            await self._execute_extraction(run)
            return
        if run.run_type == "mail_reply_draft":
            await self._execute_reply_draft(run)
            return
        raise MailTaskError(f"不支持的邮件任务类型：{run.run_type}")

    async def mark_failed(self, run: ClaimedRun, message: str) -> None:
        """将邮件派生记录同步标记失败；任务终态由生命周期服务统一处理。"""
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                await self._repository.mark_mail_task_failed(
                    session,
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    run_type=run.run_type,
                    message=message,
                )

    async def _execute_extraction(self, run: ClaimedRun) -> None:
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                source = await self._repository.load_extraction_input(
                    session, run_id=run.id, tenant_id=run.tenant_id, user_id=run.user_id
                )
        if source is None:
            raise MailTaskError("邮件摘要任务上下文不存在")
        email = await self._email_client.get_message(
            ToolContext(run.tenant_id, run.user_id, run.id, source["connection_id"]),
            source["provider_message_id"],
        )
        step_id = await self._start_llm_step(run, context, "正在生成邮件摘要")
        result = _parse_extraction(
            await self._llm_client.complete(
                [
                    LLMMessage(role="system", content=_EXTRACTION_SYSTEM_PROMPT),
                    LLMMessage(role="user", content=_mail_prompt(email)),
                ]
            )
        )
        content_hash = sha256(str(email.get("body_text", "")).encode("utf-8")).hexdigest()
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                await self._repository.mark_extraction_succeeded(
                    session,
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                    extraction_id=source["extraction_id"],
                    email_id=source["email_id"],
                    content_hash=content_hash,
                    key_points=result["key_points"],
                    due_date_candidates=result["due_date_candidates"],
                    todos=result["todos"],
                    model_provider=self._llm_client.provider_name,
                    model_name=self._llm_client.model_name,
                )
                await self._runs.finish_step(session, step_id=step_id, run_id=run.id, tenant_id=run.tenant_id, status="succeeded", detail="邮件摘要已生成")
                await self._append_event(session, run, "mail_extraction_completed", {
                    "email_id": str(source["email_id"]), "extraction_id": str(source["extraction_id"]),
                    "key_points": result["key_points"], "todos": result["todos"],
                    "due_date_candidates": result["due_date_candidates"],
                })
                await self._append_event(session, run, "run_completed", {"status": "completed", "result_summary": "邮件摘要已生成"})

    async def _execute_reply_draft(self, run: ClaimedRun) -> None:
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                source = await self._repository.load_reply_input(
                    session, run_id=run.id, tenant_id=run.tenant_id, user_id=run.user_id
                )
        if source is None:
            raise MailTaskError("邮件回复草稿任务上下文不存在")
        email = await self._email_client.get_message(
            ToolContext(run.tenant_id, run.user_id, run.id, source["connection_id"]),
            source["provider_message_id"],
        )
        step_id = await self._start_llm_step(run, context, "正在起草回复")
        reply = _parse_reply(
            await self._llm_client.complete(
                [
                    LLMMessage(role="system", content=_REPLY_SYSTEM_PROMPT),
                    LLMMessage(
                        role="user",
                        content=_reply_prompt(email, source.get("key_points"), source.get("instruction")),
                    ),
                ]
            )
        )
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                draft_id = await self._repository.mark_reply_succeeded(
                    session,
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                    email_id=source["email_id"],
                    extraction_id=source["extraction_id"],
                    connection_id=source["connection_id"],
                    reply=reply,
                )
                await self._runs.finish_step(session, step_id=step_id, run_id=run.id, tenant_id=run.tenant_id, status="succeeded", detail="回复草稿已生成")
                await self._append_event(session, run, "mail_reply_draft_completed", {
                    "draft_id": str(draft_id), "to": reply["to"], "cc": reply["cc"],
                    "subject": reply["subject"], "body": reply["body"],
                })
                await self._append_event(session, run, "run_completed", {"status": "completed", "result_summary": "邮件回复草稿已生成"})

    async def _start_llm_step(self, run: ClaimedRun, context: TenantContext, label: str):
        """登记模型步骤并将默认标签替换为邮件任务的用户可见进度。"""
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                step_id = await self._runs.create_running_llm_step(session, run_id=run.id, tenant_id=run.tenant_id)
                await session.execute(text("UPDATE run_steps SET label = :label WHERE id = :step_id"), {"label": label, "step_id": step_id})
                await session.execute(text("UPDATE agent_runs SET current_stage = :label WHERE id = :run_id"), {"label": label, "run_id": run.id})
                await self._append_event(session, run, "progress_updated", {"step_id": str(step_id), "label": label, "status": "running"})
        return step_id

    async def _append_event(self, session, run: ClaimedRun, event_type: str, payload: dict[str, Any]) -> None:
        if self._events is None:
            return
        event = await self._runs.append_event(session, run_id=run.id, tenant_id=run.tenant_id, event_type=event_type, payload=payload)
        self._events.publish_after_commit(event)


_EXTRACTION_SYSTEM_PROMPT = """你是邮件信息提取器。只输出 JSON，不要 Markdown。格式：
{"key_points":["不超过200字"],"todos":[{"content":"待办","due_at":"ISO 8601 或 null","due_text":"原文日期或 null","is_inferred":true}],"due_date_candidates":["ISO 8601"]}
不要编造事实；没有明确待办时 todos 返回空数组。"""

_REPLY_SYSTEM_PROMPT = """你是商务邮件起草助手。只输出 JSON，不要 Markdown。格式：
{"to":["email"],"cc":[],"subject":"主题","body":"正文"}
只起草，不声称已经发送。收件人应使用原邮件发件人或原邮件明确给出的地址。"""


def _mail_prompt(email: dict[str, Any]) -> str:
    return json.dumps({key: email.get(key) for key in ("sender_name", "sender_email", "subject", "received_at", "body_text")}, ensure_ascii=False)


def _reply_prompt(email: dict[str, Any], key_points: Any, instruction: Any) -> str:
    return json.dumps({"mail": json.loads(_mail_prompt(email)), "key_points": key_points or [], "instruction": instruction}, ensure_ascii=False)


def _parse_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise MailTaskError("模型未返回合法 JSON") from error
    if not isinstance(value, dict):
        raise MailTaskError("模型返回格式无效")
    return value


def _parse_extraction(raw: str) -> dict[str, Any]:
    value = _parse_json(raw)
    points = value.get("key_points", [])
    todos = value.get("todos", [])
    due_dates = value.get("due_date_candidates", [])
    if (
        not isinstance(points, list)
        or not isinstance(due_dates, list)
        or not isinstance(todos, list)
        or not all(isinstance(item, str) for item in points + due_dates)
    ):
        raise MailTaskError("模型摘要字段格式无效")
    normalized_todos = []
    for todo in todos[:50]:
        if not isinstance(todo, dict) or not isinstance(todo.get("content"), str) or not todo["content"].strip():
            raise MailTaskError("模型待办字段格式无效")
        due_at = todo.get("due_at")
        due_text = todo.get("due_text")
        if due_at is not None and not isinstance(due_at, str):
            raise MailTaskError("模型待办截止时间格式无效")
        if due_text is not None and not isinstance(due_text, str):
            raise MailTaskError("模型待办截止时间文本格式无效")
        normalized_todos.append({"content": todo["content"].strip(), "due_at": due_at, "due_text": due_text, "is_inferred": bool(todo.get("is_inferred", True))})
    return {"key_points": [item[:1000] for item in points[:20]], "todos": normalized_todos, "due_date_candidates": due_dates[:20]}


def _parse_reply(raw: str) -> dict[str, Any]:
    value = _parse_json(raw)
    to, cc, subject, body = value.get("to"), value.get("cc", []), value.get("subject"), value.get("body")
    if not isinstance(to, list) or not to or not all(isinstance(item, str) and "@" in item for item in to):
        raise MailTaskError("模型未返回有效收件人")
    if not isinstance(cc, list) or not all(isinstance(item, str) and "@" in item for item in cc):
        raise MailTaskError("模型未返回有效抄送人")
    if not isinstance(subject, str) or not subject.strip() or not isinstance(body, str) or not body.strip():
        raise MailTaskError("模型未返回有效邮件主题或正文")
    return {"to": list(dict.fromkeys(item.strip().lower() for item in to)), "cc": list(dict.fromkeys(item.strip().lower() for item in cc)), "subject": subject.strip(), "body": body.strip()}
