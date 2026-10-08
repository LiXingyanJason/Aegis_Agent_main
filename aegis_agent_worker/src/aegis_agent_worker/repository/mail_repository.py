"""邮件摘要、回复草稿任务的 Worker 侧持久化。"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class MailTaskRepository:
    """只处理邮件后台任务输入、派生结果与任务终态。"""

    async def load_extraction_input(
        self, session: AsyncSession, *, run_id: UUID, tenant_id: UUID, user_id: UUID
    ) -> dict[str, Any] | None:
        """读取待提取邮件及其来源连接；不从数据库读取邮件正文。"""
        result = await session.execute(
            text(
                "SELECT me.id AS extraction_id, em.id AS email_id, em.connection_id, "
                "em.provider_message_id, em.source_version FROM mail_extractions me "
                "JOIN email_messages em ON em.id = me.email_id "
                "WHERE me.run_id = :run_id AND me.tenant_id = :tenant_id "
                "AND me.user_id = :user_id AND me.status IN ('queued','running')"
            ),
            {"run_id": run_id, "tenant_id": tenant_id, "user_id": user_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def load_reply_input(
        self, session: AsyncSession, *, run_id: UUID, tenant_id: UUID, user_id: UUID
    ) -> dict[str, Any] | None:
        """读取回复起草请求、原邮件快照和可选摘要结果。"""
        result = await session.execute(
            text(
                "SELECT rr.email_id, rr.extraction_id, rr.instruction, em.connection_id, "
                "em.provider_message_id, me.key_points, me.due_date_candidates "
                "FROM mail_reply_draft_requests rr JOIN email_messages em ON em.id = rr.email_id "
                "LEFT JOIN mail_extractions me ON me.id = rr.extraction_id "
                "WHERE rr.run_id = :run_id AND rr.tenant_id = :tenant_id "
                "AND rr.user_id = :user_id AND rr.status IN ('queued','running')"
            ),
            {"run_id": run_id, "tenant_id": tenant_id, "user_id": user_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def mark_extraction_succeeded(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        extraction_id: UUID,
        email_id: UUID,
        content_hash: str,
        key_points: list[str],
        due_date_candidates: list[str],
        todos: list[dict[str, Any]],
        model_provider: str,
        model_name: str,
    ) -> None:
        """保存一次提取结果及候选待办，并完成后台任务。"""
        await session.execute(
            text(
                "UPDATE mail_extractions SET status = 'succeeded', content_hash = :content_hash, "
                "key_points = CAST(:key_points AS jsonb), due_date_candidates = CAST(:due_dates AS jsonb), "
                "model_provider = :model_provider, model_name = :model_name, generated_at = now() "
                "WHERE id = :extraction_id AND run_id = :run_id AND tenant_id = :tenant_id"
            ),
            {
                "extraction_id": extraction_id,
                "run_id": run_id,
                "tenant_id": tenant_id,
                "content_hash": content_hash,
                "key_points": json.dumps(key_points, ensure_ascii=False),
                "due_dates": json.dumps(due_date_candidates, ensure_ascii=False),
                "model_provider": model_provider,
                "model_name": model_name,
            },
        )
        await session.execute(
            text("DELETE FROM mail_extraction_todos WHERE extraction_id = :extraction_id"),
            {"extraction_id": extraction_id},
        )
        for index, todo in enumerate(todos):
            await session.execute(
                text(
                    "INSERT INTO mail_extraction_todos (tenant_id, extraction_id, item_index, "
                    "todo_text, due_at, due_text, is_inferred) VALUES (:tenant_id, :extraction_id, :item_index, "
                    ":todo_text, CAST(CAST(:due_at AS text) AS timestamptz), :due_text, :is_inferred)"
                ),
                {
                    "tenant_id": tenant_id,
                    "extraction_id": extraction_id,
                    "item_index": index,
                    "todo_text": todo["content"][:2000],
                    "due_at": todo.get("due_at"),
                    "due_text": todo.get("due_text"),
                    "is_inferred": todo.get("is_inferred", True),
                },
            )
        await session.execute(
            text("UPDATE email_messages SET extraction_status = 'completed', updated_at = now() WHERE id = :email_id AND tenant_id = :tenant_id"),
            {"email_id": email_id, "tenant_id": tenant_id},
        )
        await self._complete_run(session, run_id=run_id, tenant_id=tenant_id, summary="邮件摘要已生成")

    async def mark_reply_succeeded(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        email_id: UUID,
        extraction_id: UUID | None,
        connection_id: UUID,
        reply: dict[str, Any],
    ) -> UUID:
        """持久化站内回复草稿及收件人；不调用外部邮件发送。"""
        result = await session.execute(
            text(
                "INSERT INTO mail_drafts (tenant_id, user_id, source_email_id, source_extraction_id, "
                "source_run_id, send_connection_id, subject, body) VALUES (:tenant_id, :user_id, "
                ":email_id, :extraction_id, :run_id, :connection_id, :subject, :body) RETURNING id"
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "email_id": email_id,
                "extraction_id": extraction_id,
                "run_id": run_id,
                "connection_id": connection_id,
                "subject": reply["subject"][:998],
                "body": reply["body"][:65536],
            },
        )
        draft_id = result.mappings().one()["id"]
        for recipient_type in ("to", "cc"):
            for email in reply.get(recipient_type, []):
                await session.execute(
                    text("INSERT INTO mail_draft_recipients (tenant_id, draft_id, recipient_type, email) VALUES (:tenant_id, :draft_id, :recipient_type, :email)"),
                    {"tenant_id": tenant_id, "draft_id": draft_id, "recipient_type": recipient_type, "email": email[:320]},
                )
        await session.execute(
            text("UPDATE mail_reply_draft_requests SET status = 'succeeded', updated_at = now() WHERE run_id = :run_id AND tenant_id = :tenant_id"),
            {"run_id": run_id, "tenant_id": tenant_id},
        )
        await self._complete_run(session, run_id=run_id, tenant_id=tenant_id, summary="邮件回复草稿已生成")
        return draft_id

    async def mark_mail_task_failed(self, session: AsyncSession, *, run_id: UUID, tenant_id: UUID, run_type: str, message: str) -> None:
        """同步邮件派生记录的失败状态，供页面显示重新生成入口。"""
        if run_type == "mail_extraction":
            await session.execute(
                text("UPDATE mail_extractions SET status = 'failed', failure_reason = :message WHERE run_id = :run_id AND tenant_id = :tenant_id"),
                {"run_id": run_id, "tenant_id": tenant_id, "message": message[:1000]},
            )
            await session.execute(
                text(
                    "UPDATE email_messages SET extraction_status = 'failed', updated_at = now() "
                    "WHERE id IN (SELECT email_id FROM mail_extractions "
                    "WHERE run_id = :run_id AND tenant_id = :tenant_id) "
                    "AND tenant_id = :tenant_id"
                ),
                {"run_id": run_id, "tenant_id": tenant_id},
            )
        elif run_type == "mail_reply_draft":
            await session.execute(
                text("UPDATE mail_reply_draft_requests SET status = 'failed' WHERE run_id = :run_id AND tenant_id = :tenant_id"),
                {"run_id": run_id, "tenant_id": tenant_id},
            )
        # 邮件任务的 LLM 步骤由 MailTaskService 创建；失败时不能遗留 running 状态。
        await session.execute(
            text(
                "UPDATE run_steps SET status = 'failed', detail = :message, finished_at = now() "
                "WHERE run_id = :run_id AND tenant_id = :tenant_id AND status = 'running'"
            ),
            {"run_id": run_id, "tenant_id": tenant_id, "message": message[:1000]},
        )

    async def _complete_run(self, session: AsyncSession, *, run_id: UUID, tenant_id: UUID, summary: str) -> None:
        await session.execute(
            text("UPDATE agent_runs SET status = 'completed', current_stage = '已完成', result_summary = :summary, finished_at = now(), updated_at = now() WHERE id = :run_id AND tenant_id = :tenant_id"),
            {"run_id": run_id, "tenant_id": tenant_id, "summary": summary},
        )
