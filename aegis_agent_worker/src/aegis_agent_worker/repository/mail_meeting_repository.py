"""会议要求来源的SQL核验；无LLM/网络调用，不读正文。"""
from sqlalchemy import text


class MailMeetingRepository:
    async def load_reference(self, session, run, reference):
        result = await session.execute(text("""
            SELECT em.id, em.connection_id, em.provider_message_id, em.source_version,
                   em.subject, em.sender_email, em.received_at
            FROM email_messages em
            JOIN provider_connections pc ON pc.id=em.connection_id
              AND pc.tenant_id=em.tenant_id AND pc.user_id=em.user_id
            WHERE em.id=:email_id AND em.tenant_id=:tenant_id AND em.user_id=:user_id
              AND em.source_version=:source_version
              AND pc.status='active' AND (pc.expires_at IS NULL OR pc.expires_at>now())
              AND pc.provider IN ('gmail','outlook_mail')
              AND pc.scopes @> CAST(:scope AS jsonb)
        """), dict(email_id=reference.id, source_version=reference.source_version,
                    tenant_id=run.tenant_id, user_id=run.user_id, scope='["mail.read"]'))
        row = result.mappings().first()
        return dict(row) if row is not None else None
