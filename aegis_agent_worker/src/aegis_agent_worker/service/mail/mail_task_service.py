"""邮件摘要与回复草稿的 Worker 应用服务。"""

from typing import Any

from sqlalchemy import text

from aegis_agent_worker.config.database import Database, TenantContext, tenant_transaction
from aegis_agent_worker.event.run_event import RunEventPublisher
from aegis_agent_worker.agent.specialists.mail_agent import (
    MailAgentError,
    _parse_extraction,
    _parse_reply,
)
from aegis_agent_worker.repository.mail_repository import MailTaskRepository
from aegis_agent_worker.repository.run_repository import AgentRunRepository, ClaimedRun
from aegis_agent_worker.tool.contracts import ToolInvocation
from aegis_agent_worker.tool.gateway import ToolGateway
# 保持现有调用方及测试的异常名称兼容；语义推理错误由 MailAgent 定义。
MailTaskError = MailAgentError


class MailTaskService:
    """为邮件子图提供摘要、回复草稿及其持久化业务动作。"""

    def __init__(
        self,
        database: Database,
        runs: AgentRunRepository,
        tools: ToolGateway,
        events: RunEventPublisher | None = None,
        repository: MailTaskRepository | None = None,
    ) -> None:
        self._database = database
        self._runs = runs
        self._tools = tools
        self._events = events
        self._repository = repository or MailTaskRepository()

    async def ensure_extraction_ready(self, run: ClaimedRun) -> None:
        """确认摘要任务仍存在且属于当前用户；不读取邮件正文，也不产生外部副作用。"""
        if run.run_type != "mail_extraction":
            raise MailTaskError(f"邮件摘要节点收到不匹配的任务类型：{run.run_type}")
        source = await self._load_extraction_source(run)
        if source is None:
            raise MailTaskError("邮件摘要任务上下文不存在")

    async def ensure_reply_draft_ready(self, run: ClaimedRun) -> None:
        """确认回复草稿任务仍存在且属于当前用户；不读取邮件正文，也不产生外部副作用。"""
        if run.run_type != "mail_reply_draft":
            raise MailTaskError(f"邮件起草节点收到不匹配的任务类型：{run.run_type}")
        source = await self._load_reply_source(run)
        if source is None:
            raise MailTaskError("邮件回复草稿任务上下文不存在")

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

    async def load_extraction_source(self, run: ClaimedRun) -> dict[str, Any]:
        """返回摘要任务的可信来源引用，供当前图节点作为局部变量使用。"""
        source = await self._load_extraction_source(run)
        if source is None:
            raise MailTaskError("邮件摘要任务上下文不存在")
        return source

    async def load_reply_source(self, run: ClaimedRun) -> dict[str, Any]:
        """返回回复任务的可信来源引用，供当前图节点作为局部变量使用。"""
        source = await self._load_reply_source(run)
        if source is None:
            raise MailTaskError("邮件回复草稿任务上下文不存在")
        return source

    async def read_mail(self, run: ClaimedRun, source: dict[str, Any]) -> dict[str, Any]:
        """经统一 ToolGateway 读取来源邮件；正文只存在于当前节点局部变量。"""
        return await self._get_message(run, source["connection_id"], source["provider_message_id"])

    async def start_generation_step(self, run: ClaimedRun, label: str):
        """登记用户可见的模型生成步骤；模型调用本身由 MailAgent 负责。"""
        return await self._start_llm_step(
            run, TenantContext(tenant_id=run.tenant_id, user_id=run.user_id), label
        )

    async def save_extraction(
        self,
        run: ClaimedRun,
        *,
        source: dict[str, Any],
        email: dict[str, Any],
        result: dict[str, Any],
        step_id,
        model_provider: str,
        model_name: str,
    ) -> None:
        """原子保存摘要、待办和步骤结果；不参与 LLM 推理。"""
        from hashlib import sha256

        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        content_hash = sha256(str(email.get("body_text", "")).encode("utf-8")).hexdigest()
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                await self._repository.mark_extraction_succeeded(
                    session, run_id=run.id, tenant_id=run.tenant_id, user_id=run.user_id,
                    extraction_id=source["extraction_id"], email_id=source["email_id"],
                    content_hash=content_hash, key_points=result["key_points"],
                    due_date_candidates=result["due_date_candidates"], todos=result["todos"],
                    model_provider=model_provider, model_name=model_name,
                )
                await self._runs.finish_step(session, step_id=step_id, run_id=run.id, tenant_id=run.tenant_id, status="succeeded", detail="邮件摘要已生成")
                await self._append_event(session, run, "mail_extraction_completed", {
                    "email_id": str(source["email_id"]), "extraction_id": str(source["extraction_id"]),
                    "key_points": result["key_points"], "todos": result["todos"],
                    "due_date_candidates": result["due_date_candidates"],
                })
                await self._append_event(session, run, "run_completed", {"status": "completed", "result_summary": "邮件摘要已生成"})

    async def save_reply_draft(
        self, run: ClaimedRun, *, source: dict[str, Any], reply: dict[str, Any], step_id
    ) -> None:
        """原子保存回复草稿和步骤结果；不参与 LLM 推理。"""
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                draft_id = await self._repository.mark_reply_succeeded(
                    session, run_id=run.id, tenant_id=run.tenant_id, user_id=run.user_id,
                    email_id=source["email_id"], extraction_id=source["extraction_id"],
                    connection_id=source["connection_id"], reply=reply,
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

    async def _load_extraction_source(self, run: ClaimedRun) -> dict[str, Any] | None:
        """在租户事务中读取摘要任务的可信邮件引用。"""
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                return await self._repository.load_extraction_input(
                    session, run_id=run.id, tenant_id=run.tenant_id, user_id=run.user_id
                )

    async def _load_reply_source(self, run: ClaimedRun) -> dict[str, Any] | None:
        """在租户事务中读取回复草稿任务的可信邮件引用与可选摘要。"""
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                return await self._repository.load_reply_input(
                    session, run_id=run.id, tenant_id=run.tenant_id, user_id=run.user_id
                )

    async def _get_message(
        self, run: ClaimedRun, connection_id, provider_message_id: str
    ) -> dict[str, Any]:
        """经统一 Gateway 校验邮箱连接后，读取邮件正文供当前后台任务使用。"""
        context = TenantContext(tenant_id=run.tenant_id, user_id=run.user_id)
        async with self._database.session() as session:
            async with tenant_transaction(session, context):
                prepared = await self._tools.prepare(
                    session,
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                    run_id=run.id,
                    invocation=ToolInvocation(
                        tool_name="mail.messages.get",
                        arguments={"provider_message_id": provider_message_id},
                    ),
                    # 连接标识来自 Aegis 本地邮件快照，而不是模型或浏览器输入。
                    required_connection_id=connection_id,
                )
        return (await self._tools.invoke(prepared)).response_payload

    async def _append_event(self, session, run: ClaimedRun, event_type: str, payload: dict[str, Any]) -> None:
        if self._events is None:
            return
        event = await self._runs.append_event(session, run_id=run.id, tenant_id=run.tenant_id, event_type=event_type, payload=payload)
        self._events.publish_after_commit(event)
