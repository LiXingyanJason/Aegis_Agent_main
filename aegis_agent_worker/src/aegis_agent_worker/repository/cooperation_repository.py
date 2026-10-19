"""固定协同交接的SQL事实；不读正文、不让模型控制身份或连接。"""
from sqlalchemy import text


class CooperationRepository:
    async def email_source(self, session, run, reference):
        result = await session.execute(text('''
            SELECT e.id,e.source_version,e.connection_id,e.provider_message_id
            FROM email_messages e WHERE e.id=:id AND e.tenant_id=:tenant AND e.user_id=:user
              AND e.source_version=:version
        '''), dict(id=reference.id,tenant=run.tenant_id,user=run.user_id,version=reference.source_version))
        row = result.mappings().first()
        return dict(row) if row else None
