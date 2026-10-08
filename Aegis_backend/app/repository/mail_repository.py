"""邮件连接与元数据快照的数据访问层。"""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any
from uuid import UUID
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.integration.email_mcp_client import EmailMCPMessage
from app.observability.structured_logging import get_current_trace_id


@dataclass(frozen=True, slots=True)
class MailConnection:
    """当前用户可用于读取邮件的有效外部连接。"""

    id: UUID
    provider: str


@dataclass(frozen=True, slots=True)
class MailSendConfirmation:
    """邮件发送确认创建后的任务与确认项标识。"""

    run_id: UUID
    approval_item_id: UUID


class MailRepository:
    """封装 provider_connections 与 email_messages 的读写。"""

    async def find_active_read_connection(
        self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID
    ) -> MailConnection | None:
        """查找有 `mail.read` scope 且未过期的邮箱连接。"""
        result = await session.execute(
            text(
                "SELECT id, provider FROM provider_connections "
                "WHERE tenant_id = :tenant_id AND user_id = :user_id "
                "  AND provider IN ('gmail', 'outlook_mail') "
                "  AND status = 'active' "
                "  AND (expires_at IS NULL OR expires_at > now()) "
                "  AND scopes @> CAST(:mail_read_scope AS jsonb) "
                "ORDER BY last_verified_at DESC NULLS LAST, created_at DESC LIMIT 1"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "mail_read_scope": '["mail.read"]'},
        )
        row = result.mappings().first()
        return None if row is None else MailConnection(id=row["id"], provider=row["provider"])

    async def find_active_send_connection(
        self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID
    ) -> MailConnection | None:
        """查找拥有 `mail.send` scope 的有效工作邮箱连接。"""
        result = await session.execute(
            text(
                "SELECT id, provider FROM provider_connections "
                "WHERE tenant_id = :tenant_id AND user_id = :user_id "
                "  AND provider IN ('gmail', 'outlook_mail') AND status = 'active' "
                "  AND (expires_at IS NULL OR expires_at > now()) "
                "  AND scopes @> CAST(:mail_send_scope AS jsonb) "
                "ORDER BY last_verified_at DESC NULLS LAST, created_at DESC LIMIT 1"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "mail_send_scope": '[\"mail.send\"]'},
        )
        row = result.mappings().first()
        return None if row is None else MailConnection(id=row["id"], provider=row["provider"])

    async def create_send_confirmation(
        self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID, draft_id: UUID
    ) -> MailSendConfirmation | str | None:
        """冻结草稿版本并创建一个等待逐项确认的 mail_send 任务。"""
        locked = await session.execute(
            text(
                "SELECT id, subject, body, version, status FROM mail_drafts WHERE id = :draft_id "
                "AND tenant_id = :tenant_id AND user_id = :user_id FOR UPDATE"
            ),
            {"draft_id": draft_id, "tenant_id": tenant_id, "user_id": user_id},
        )
        draft = locked.mappings().first()
        if draft is None:
            return None
        if draft["status"] != "draft":
            return "not_sendable"
        connection = await self.find_active_send_connection(
            session, tenant_id=tenant_id, user_id=user_id
        )
        if connection is None:
            return "connection_required"
        recipients = await self._list_draft_recipients(session, tenant_id=tenant_id, draft_id=draft_id)
        if not recipients["to"]:
            return "not_sendable"
        preview = {
            "to": recipients["to"], "cc": recipients["cc"], "subject": draft["subject"],
            "body_preview": draft["body"][:3000], "draft_version": draft["version"],
        }
        parameters_hash = sha256(
            json.dumps(preview, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        run_result = await session.execute(
            text(
                "INSERT INTO agent_runs (tenant_id, user_id, run_type, status, request_id, trace_id, intent, current_stage) "
                "VALUES (:tenant_id, :user_id, 'mail_send', 'waiting_confirmation', :request_id, :trace_id, "
                "'mail_send', '等待发送确认') RETURNING id"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "request_id": uuid4().hex,
             "trace_id": get_current_trace_id(),},
        )
        run_id = run_result.mappings().one()["id"]
        await session.execute(
            text("UPDATE mail_drafts SET status = 'pending_approval', send_connection_id = :connection_id, updated_at = now() WHERE id = :draft_id"),
            {"draft_id": draft_id, "connection_id": connection.id},
        )
        approval_result = await session.execute(
            text(
                "INSERT INTO approval_items (tenant_id, user_id, run_id, action, risk_level, resource_type, resource_id, "
                "title, preview_snapshot, parameters_hash, draft_version, expires_at) "
                "VALUES (:tenant_id, :user_id, :run_id, 'mail.messages.send', 'sensitive', 'mail_draft', :draft_id, "
                ":title, CAST(:preview AS jsonb), :parameters_hash, :draft_version, now() + interval '24 hours') RETURNING id"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "run_id": run_id, "draft_id": draft_id,
             "title": f"发送邮件：{draft['subject']}"[:256],
             "preview": json.dumps(preview, ensure_ascii=False), "parameters_hash": parameters_hash,
             "draft_version": draft["version"]},
        )
        approval_item_id = approval_result.mappings().one()["id"]
        await session.execute(
            text(
                "INSERT INTO run_events (tenant_id, run_id, event_no, event_type, payload) "
                "VALUES (:tenant_id, :run_id, 1, 'approval_required', CAST(:payload AS jsonb))"
            ),
            {"tenant_id": tenant_id, "run_id": run_id, "payload": json.dumps({
                "approval_item_id": str(approval_item_id), "resource_type": "mail_draft",
                "resource_id": str(draft_id), "title": f"发送邮件：{draft['subject']}"[:256],
                "status": "pending",
            }, ensure_ascii=False)},
        )
        return MailSendConfirmation(run_id=run_id, approval_item_id=approval_item_id)

    async def upsert_messages(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        connection_id: UUID,
        messages: list[EmailMCPMessage],
    ) -> None:
        """以连接和外部邮件 ID 为唯一键更新列表快照，不持久化邮件正文。"""
        for message in messages:
            metadata = message.model_dump(mode="json")
            content_hash = sha256(
                json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            await session.execute(
                text(
                    "INSERT INTO email_messages ("
                    "tenant_id, user_id, connection_id, provider_message_id, provider_thread_id, "
                    "sender_name, sender_email, subject, received_at, source_version, content_hash, "
                    "list_preview, snapshot_at) "
                    "VALUES (:tenant_id, :user_id, :connection_id, :provider_message_id, "
                    ":provider_thread_id, :sender_name, :sender_email, :subject, "
                    "CAST(:received_at AS timestamptz), :source_version, :content_hash, "
                    ":list_preview, now()) "
                    "ON CONFLICT (tenant_id, user_id, connection_id, provider_message_id) "
                    "DO UPDATE SET provider_thread_id = EXCLUDED.provider_thread_id, "
                    "sender_name = EXCLUDED.sender_name, sender_email = EXCLUDED.sender_email, "
                    "subject = EXCLUDED.subject, received_at = EXCLUDED.received_at, "
                    "content_hash = EXCLUDED.content_hash, list_preview = EXCLUDED.list_preview, "
                    "extraction_status = CASE "
                    "  WHEN email_messages.source_version <> EXCLUDED.source_version "
                    "  THEN 'stale' ELSE email_messages.extraction_status END, "
                    "source_version = EXCLUDED.source_version, snapshot_at = now(), updated_at = now()"
                ),
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "connection_id": connection_id,
                    "provider_message_id": message.provider_message_id,
                    "provider_thread_id": message.provider_thread_id,
                    "sender_name": message.sender_name,
                    "sender_email": message.sender_email,
                    "subject": message.subject,
                    # asyncpg 对 timestamptz 绑定要求原生 datetime，不能先序列化为 ISO 字符串。
                    "received_at": message.received_at,
                    "source_version": message.source_version,
                    "content_hash": content_hash,
                    "list_preview": message.list_preview,
                },
            )

    async def list_messages(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        connection_id: UUID,
    ) -> list[dict[str, Any]]:
        """读取当前邮箱连接已保存的元数据快照。"""
        result = await session.execute(
            text(
                "SELECT id, provider_thread_id, sender_name, sender_email, subject, received_at, "
                "source_version, extraction_status, snapshot_at "
                "FROM email_messages "
                "WHERE tenant_id = :tenant_id AND user_id = :user_id "
                "  AND connection_id = :connection_id "
                "ORDER BY received_at DESC, created_at DESC"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "connection_id": connection_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def find_owned_message_detail_reference(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        email_id: UUID,
        connection_id: UUID,
    ) -> dict[str, Any] | None:
        """读取受当前邮箱连接约束的邮件快照，供详情工具调用前授权校验。"""
        result = await session.execute(
            text(
                "SELECT id, connection_id, provider_message_id FROM email_messages "
                "WHERE id = :email_id AND tenant_id = :tenant_id AND user_id = :user_id "
                "AND connection_id = :connection_id"
            ),
            {
                "email_id": email_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "connection_id": connection_id,
            },
        )
        row = result.mappings().first()
        return None if row is None else dict(row)

    async def latest_snapshot_at(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        connection_id: UUID,
    ) -> datetime | None:
        """返回本次邮箱快照中最新的同步时间。"""
        result = await session.execute(
            text(
                "SELECT MAX(snapshot_at) AS snapshot_at FROM email_messages "
                "WHERE tenant_id = :tenant_id AND user_id = :user_id "
                "  AND connection_id = :connection_id"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "connection_id": connection_id},
        )
        return result.mappings().one()["snapshot_at"]

    async def list_drafts(
        self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID
    ) -> list[dict[str, Any]]:
        """列出当前用户未发送的站内邮件草稿，不读取完整正文。"""
        result = await session.execute(
            text(
                "SELECT id, source_email_id, subject, version, status, updated_at "
                "FROM mail_drafts WHERE tenant_id = :tenant_id AND user_id = :user_id "
                "  AND status IN ('draft', 'pending_approval', 'failed') "
                "ORDER BY updated_at DESC"
            ),
            {"tenant_id": tenant_id, "user_id": user_id},
        )
        drafts = [dict(row) for row in result.mappings().all()]
        for draft in drafts:
            draft["recipients"] = await self._list_draft_recipients(
                session, tenant_id=tenant_id, draft_id=draft["id"]
            )
        return drafts

    async def find_draft(
        self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID, draft_id: UUID
    ) -> dict[str, Any] | None:
        """读取拥有者的完整草稿及其收件人。"""
        result = await session.execute(
            text(
                "SELECT id, source_email_id, subject, body, version, status, updated_at "
                "FROM mail_drafts WHERE id = :draft_id AND tenant_id = :tenant_id "
                "  AND user_id = :user_id"
            ),
            {"draft_id": draft_id, "tenant_id": tenant_id, "user_id": user_id},
        )
        row = result.mappings().first()
        if row is None:
            return None
        draft = dict(row)
        draft["recipients"] = await self._list_draft_recipients(
            session, tenant_id=tenant_id, draft_id=draft_id
        )
        return draft

    async def update_draft(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        draft_id: UUID,
        to: list[str],
        cc: list[str],
        subject: str,
        body: str,
    ) -> dict[str, Any] | str | None:
        """更新 draft 状态草稿；变更时作废所有尚待确认的旧发送项。"""
        locked = await session.execute(
            text(
                "SELECT id, status FROM mail_drafts WHERE id = :draft_id "
                "AND tenant_id = :tenant_id AND user_id = :user_id FOR UPDATE"
            ),
            {"draft_id": draft_id, "tenant_id": tenant_id, "user_id": user_id},
        )
        existing = locked.mappings().first()
        if existing is None:
            return None
        if existing["status"] != "draft":
            return "not_editable"
        await session.execute(
            text(
                "UPDATE approval_items SET status = 'invalidated', invalidated_at = now(), "
                "updated_at = now() WHERE tenant_id = :tenant_id AND user_id = :user_id "
                "AND resource_type = 'mail_draft' AND resource_id = :draft_id AND status = 'pending'"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "draft_id": draft_id},
        )
        await session.execute(
            text(
                "UPDATE mail_drafts SET subject = :subject, body = :body, version = version + 1, "
                "updated_at = now() WHERE id = :draft_id AND tenant_id = :tenant_id "
                "AND user_id = :user_id"
            ),
            {
                "draft_id": draft_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "subject": subject,
                "body": body,
            },
        )
        await session.execute(
            text(
                "DELETE FROM mail_draft_recipients WHERE tenant_id = :tenant_id "
                "AND draft_id = :draft_id"
            ),
            {"tenant_id": tenant_id, "draft_id": draft_id},
        )
        for recipient_type, addresses in (("to", to), ("cc", cc)):
            for email in addresses:
                await session.execute(
                    text(
                        "INSERT INTO mail_draft_recipients "
                        "(tenant_id, draft_id, recipient_type, email) "
                        "VALUES (:tenant_id, :draft_id, :recipient_type, :email)"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "draft_id": draft_id,
                        "recipient_type": recipient_type,
                        "email": email,
                    },
                )
        return await self.find_draft(
            session, tenant_id=tenant_id, user_id=user_id, draft_id=draft_id
        )

    async def list_sent_messages(
        self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID
    ) -> list[dict[str, Any]]:
        """列出当前用户发送成功或失败后的已发送记录。"""
        result = await session.execute(
            text(
                "SELECT sm.id, sm.draft_id, sm.provider_message_id, sm.subject, "
                "sm.recipient_summary, sm.sent_at, sm.status, md.source_run_id AS run_id "
                "FROM sent_mail_messages sm JOIN mail_drafts md ON md.id = sm.draft_id "
                "WHERE sm.tenant_id = :tenant_id AND sm.user_id = :user_id "
                "ORDER BY sm.sent_at DESC"
            ),
            {"tenant_id": tenant_id, "user_id": user_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def create_todo_drafts(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        email_id: UUID,
        extraction_id: UUID,
        todo_indexes: list[int],
    ) -> list[dict[str, Any]] | None:
        """将已生成的待办候选复制为站内草稿；重复点击复用既有草稿。"""
        extraction_result = await session.execute(
            text(
                "SELECT id FROM mail_extractions WHERE id = :extraction_id "
                "AND email_id = :email_id AND tenant_id = :tenant_id AND user_id = :user_id "
                "AND status = 'succeeded'"
            ),
            {
                "extraction_id": extraction_id,
                "email_id": email_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
            },
        )
        if extraction_result.mappings().first() is None:
            return None

        todos_result = await session.execute(
            text(
                "SELECT id, item_index, todo_text, due_at FROM mail_extraction_todos "
                "WHERE tenant_id = :tenant_id AND extraction_id = :extraction_id "
                "AND item_index = ANY(CAST(:todo_indexes AS integer[])) "
                "ORDER BY item_index"
            ),
            {"tenant_id": tenant_id, "extraction_id": extraction_id, "todo_indexes": todo_indexes},
        )
        todos = [dict(row) for row in todos_result.mappings().all()]
        if len(todos) != len(set(todo_indexes)):
            return None

        drafts: list[dict[str, Any]] = []
        for todo in todos:
            result = await session.execute(
                text(
                    "INSERT INTO todo_drafts (tenant_id, user_id, email_id, extraction_id, "
                    "extraction_todo_id, content, due_at) "
                    "VALUES (:tenant_id, :user_id, :email_id, :extraction_id, "
                    ":extraction_todo_id, :content, :due_at) "
                    "ON CONFLICT (user_id, extraction_todo_id) DO UPDATE "
                    "SET updated_at = todo_drafts.updated_at "
                    "RETURNING id, email_id, extraction_id, content, due_at, status, created_at"
                ),
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "email_id": email_id,
                    "extraction_id": extraction_id,
                    "extraction_todo_id": todo["id"],
                    "content": todo["todo_text"],
                    "due_at": todo["due_at"],
                },
            )
            drafts.append(dict(result.mappings().one()))
            await session.execute(
                text(
                    "UPDATE mail_extraction_todos SET status = 'drafted' "
                    "WHERE id = :todo_id AND tenant_id = :tenant_id"
                ),
                {"todo_id": todo["id"], "tenant_id": tenant_id},
            )
        return drafts

    async def find_owned_email(
        self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID, email_id: UUID
    ) -> dict[str, Any] | None:
        """读取当前用户的一封邮件快照及所属连接，供异步任务创建时校验。"""
        result = await session.execute(
            text(
                "SELECT id, connection_id, source_version, content_hash, extraction_status "
                "FROM email_messages WHERE id = :email_id AND tenant_id = :tenant_id "
                "AND user_id = :user_id"
            ),
            {"email_id": email_id, "tenant_id": tenant_id, "user_id": user_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def find_reusable_extraction(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        email_id: UUID,
        source_version: str,
        extractor_version: str,
    ) -> dict[str, Any] | None:
        """查找同来源版本、同提取器版本的成功提取结果。"""
        result = await session.execute(
            text(
                "SELECT id, key_points, due_date_candidates, generated_at "
                "FROM mail_extractions WHERE tenant_id = :tenant_id AND user_id = :user_id "
                "AND email_id = :email_id AND source_version = :source_version "
                "AND extractor_version = :extractor_version AND status = 'succeeded' "
                "ORDER BY generated_at DESC NULLS LAST, created_at DESC LIMIT 1"
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "email_id": email_id,
                "source_version": source_version,
                "extractor_version": extractor_version,
            },
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def list_extraction_todos(
        self, session: AsyncSession, *, tenant_id: UUID, extraction_id: UUID
    ) -> list[dict[str, Any]]:
        """读取已完成摘要的待办候选，供缓存命中时直接返回页面。"""
        result = await session.execute(
            text(
                "SELECT item_index, todo_text, due_at, due_text, is_inferred, status "
                "FROM mail_extraction_todos WHERE tenant_id = :tenant_id "
                "AND extraction_id = :extraction_id ORDER BY item_index"
            ),
            {"tenant_id": tenant_id, "extraction_id": extraction_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def create_extraction_run(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        email: dict[str, Any],
    ) -> tuple[UUID, UUID]:
        """创建 mail_extraction 任务及其待处理的提取记录。"""
        run_result = await session.execute(
            text(
                "INSERT INTO agent_runs (tenant_id, user_id, run_type, status, request_id, trace_id, intent) "
                "VALUES (:tenant_id, :user_id, 'mail_extraction', 'queued', :request_id, :trace_id, "
                "'mail_extraction') RETURNING id"
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "request_id": uuid4().hex,
                "trace_id": get_current_trace_id(),
            },
        )
        run_id = run_result.mappings().one()["id"]
        extraction_result = await session.execute(
            text(
                "INSERT INTO mail_extractions (tenant_id, user_id, email_id, run_id, source_version, "
                "content_hash, extractor_version, prompt_version, status) VALUES "
                "(:tenant_id, :user_id, :email_id, :run_id, :source_version, :content_hash, "
                "'mail-extract-v1', 'mail-extract-prompt-v1', 'queued') RETURNING id"
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "email_id": email["id"],
                "run_id": run_id,
                "source_version": email["source_version"],
                "content_hash": email["content_hash"],
            },
        )
        await session.execute(
            text(
                "UPDATE email_messages SET extraction_status = 'running', updated_at = now() "
                "WHERE id = :email_id AND tenant_id = :tenant_id"
            ),
            {"email_id": email["id"], "tenant_id": tenant_id},
        )
        return run_id, extraction_result.mappings().one()["id"]

    async def create_reply_draft_run(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        user_id: UUID,
        email: dict[str, Any],
        extraction_id: UUID | None,
        instruction: str | None,
    ) -> UUID | None:
        """创建 mail_reply_draft 运行及其起草请求；无效提取结果返回 None。"""
        if extraction_id is not None:
            extraction = await session.execute(
                text(
                    "SELECT id FROM mail_extractions WHERE id = :extraction_id AND email_id = :email_id "
                    "AND tenant_id = :tenant_id AND user_id = :user_id AND status = 'succeeded'"
                ),
                {
                    "extraction_id": extraction_id,
                    "email_id": email["id"],
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                },
            )
            if extraction.mappings().first() is None:
                return None
        run_result = await session.execute(
            text(
                "INSERT INTO agent_runs (tenant_id, user_id, run_type, status, request_id, trace_id, intent) "
                "VALUES (:tenant_id, :user_id, 'mail_reply_draft', 'queued', :request_id, :trace_id, "
                "'mail_reply_draft') RETURNING id"
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "request_id": uuid4().hex,
                "trace_id": get_current_trace_id(),
            },
        )
        run_id = run_result.mappings().one()["id"]
        await session.execute(
            text(
                "INSERT INTO mail_reply_draft_requests (tenant_id, user_id, email_id, extraction_id, run_id, instruction) "
                "VALUES (:tenant_id, :user_id, :email_id, :extraction_id, :run_id, :instruction)"
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "email_id": email["id"],
                "extraction_id": extraction_id,
                "run_id": run_id,
                "instruction": instruction,
            },
        )
        return run_id

    async def _list_draft_recipients(
        self, session: AsyncSession, *, tenant_id: UUID, draft_id: UUID
    ) -> dict[str, list[str]]:
        """按收件人类型组装草稿地址，防止列表接口暴露正文。"""
        result = await session.execute(
            text(
                "SELECT recipient_type, email FROM mail_draft_recipients "
                "WHERE tenant_id = :tenant_id AND draft_id = :draft_id "
                "ORDER BY created_at"
            ),
            {"tenant_id": tenant_id, "draft_id": draft_id},
        )
        grouped: dict[str, list[str]] = {"to": [], "cc": [], "bcc": []}
        for row in result.mappings().all():
            grouped[row["recipient_type"]].append(row["email"])
        return grouped
