"""Worker 邮件查询同步快照；与 API 共用 email_messages 唯一键及版本规则。"""

import json
from hashlib import sha256
from sqlalchemy import text


class MailQueryRepository:
    async def upsert(self, session, run, connection_id, messages):
        """全量元数据同步得到稳定 Aegis ID；正文不会进入邮件快照。"""
        rows = []
        for message in messages:
            metadata = message.model_dump(mode="json")
            digest = sha256(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            args = message.model_dump()
            args.pop("attachment_count")
            args.update(tenant_id=run.tenant_id, user_id=run.user_id,
                        connection_id=connection_id, content_hash=digest)
            result = await session.execute(text("""
                INSERT INTO email_messages
                  (tenant_id, user_id, connection_id, provider_message_id, provider_thread_id,
                   sender_name, sender_email, subject, received_at, source_version, content_hash,
                   list_preview, snapshot_at)
                VALUES (:tenant_id, :user_id, :connection_id, :provider_message_id, :provider_thread_id,
                        :sender_name, :sender_email, :subject, :received_at, :source_version, :content_hash,
                        :list_preview, now())
                ON CONFLICT (tenant_id, user_id, connection_id, provider_message_id) DO UPDATE SET
                  provider_thread_id = EXCLUDED.provider_thread_id, sender_name = EXCLUDED.sender_name,
                  sender_email = EXCLUDED.sender_email, subject = EXCLUDED.subject,
                  received_at = EXCLUDED.received_at, content_hash = EXCLUDED.content_hash,
                  list_preview = EXCLUDED.list_preview,
                  extraction_status = CASE WHEN email_messages.source_version <> EXCLUDED.source_version
                    THEN 'stale' ELSE email_messages.extraction_status END,
                  source_version = EXCLUDED.source_version, snapshot_at = now(), updated_at = now()
                RETURNING id, subject, sender_name, sender_email, received_at, source_version, extraction_status
            """), args)
            rows.append(dict(result.mappings().one()))
        return rows
