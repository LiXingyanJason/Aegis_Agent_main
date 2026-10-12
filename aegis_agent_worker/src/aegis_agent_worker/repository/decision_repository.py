"""主决策上下文的受租户限制读取；不读取邮件正文。"""

from sqlalchemy import text


class DecisionRepository:
    async def list_references(self, session, run):
        """最近邮件快照提供有限目标集；尚未接入的检索不能冒充已完成。"""
        result = await session.execute(text("""
            SELECT id, subject, source_version FROM email_messages
            WHERE tenant_id = :tenant_id AND user_id = :user_id
            ORDER BY received_at DESC, id LIMIT 20
        """), {"tenant_id": run.tenant_id, "user_id": run.user_id})
        return [
            {"resource_type": "email", "id": str(row["id"]),
             "source_version": row["source_version"], "label": row["subject"][:200]}
            for row in result.mappings().all()
        ]

    async def resolve_reference(self, session, run, reference):
        """重新核验模型引用的真实用户归属与来源版本。"""
        if reference.resource_type != "email":
            return None
        result = await session.execute(text("""
            SELECT id, source_version FROM email_messages
            WHERE id = :id AND tenant_id = :tenant_id AND user_id = :user_id
        """), {"id": reference.id, "tenant_id": run.tenant_id, "user_id": run.user_id})
        row = result.mappings().first()
        if row is None or reference.source_version != row["source_version"]:
            return None
        return {"resource_type": "email", "id": str(row["id"]), "source_version": row["source_version"]}
