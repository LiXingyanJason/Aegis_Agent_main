"""主决策上下文的受租户限制读取；不读取邮件正文。"""

from sqlalchemy import text
import json
from uuid import UUID


class DecisionRepository:
    async def list_references(self, session, run):
        """优先按当前会话最近邮件卡片顺序解析指代，并复核版本。"""
        artifact_result = await session.execute(text("""
            SELECT payload, expires_at <= now() AS expired FROM conversation_message_artifacts
            WHERE tenant_id = :tenant_id AND user_id = :user_id
              AND conversation_id = :conversation_id AND artifact_type = 'mail_candidates'
            ORDER BY created_at DESC, id DESC LIMIT 1
        """), {"tenant_id": run.tenant_id, "user_id": run.user_id, "conversation_id": run.conversation_id})
        artifact = artifact_result.mappings().first()
        if artifact is not None:
            # 空、过期或失效候选不能改用收件箱最近邮件冒充“第一封”。
            if artifact["expired"]:
                return []
            payload = artifact["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            references = []
            for item in payload.get("items", [])[:20]:
                try:
                    reference_id = UUID(item["email_id"])
                except (KeyError, ValueError, TypeError):
                    continue
                result = await session.execute(text("""
                    SELECT id, subject, source_version FROM email_messages
                    WHERE id = :email_id AND tenant_id = :tenant_id AND user_id = :user_id
                """), {"email_id": reference_id, "tenant_id": run.tenant_id, "user_id": run.user_id})
                row = result.mappings().first()
                if row is not None and row["source_version"] == item.get("source_version"):
                    references.append({
                        "resource_type": "email", "id": str(row["id"]), "source_version": row["source_version"],
                        "label": row["subject"][:200], "position": item["position"],
                        "query_id": payload.get("query_id"),
                    })
            return references
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
