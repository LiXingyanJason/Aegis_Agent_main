"""站内卡片SQL：只读取当前run所属会话，绝不回退到整个收件箱。"""
from sqlalchemy import text


class ConversationCardRepository:
    async def conversation_for_run(self, session, context):
        result = await session.execute(text("""
            SELECT conversation_id FROM agent_runs
            WHERE id=:run AND tenant_id=:tenant AND user_id=:user AND run_type='conversation'
        """), dict(run=context.run_id, tenant=context.tenant_id, user=context.user_id))
        row = result.mappings().first()
        return row["conversation_id"] if row else None

    async def list_cards(self, session, context, conversation_id, card_type, limit):
        result = await session.execute(text("""
            SELECT id,run_id,artifact_type,created_at,expires_at,expires_at <= now() AS expired,
              CASE WHEN jsonb_typeof(payload->'items')='array'
                   THEN jsonb_array_length(payload->'items') ELSE 0 END AS item_count
            FROM conversation_message_artifacts
            WHERE tenant_id=:tenant AND user_id=:user AND conversation_id=:conversation
              AND artifact_type=:type
            ORDER BY created_at DESC,id DESC LIMIT :limit
        """), dict(tenant=context.tenant_id, user=context.user_id, conversation=conversation_id,
                   type=card_type, limit=limit))
        return list(result.mappings().all())

    async def get_card(self, session, context, conversation_id, args):
        result = await session.execute(text("""
            SELECT id,run_id,artifact_type,payload,created_at,expires_at,expires_at <= now() AS expired
            FROM conversation_message_artifacts
            WHERE tenant_id=:tenant AND user_id=:user AND conversation_id=:conversation
              AND artifact_type=:type AND (:latest OR id=:id)
            ORDER BY created_at DESC,id DESC LIMIT 1
        """), dict(tenant=context.tenant_id, user=context.user_id, conversation=conversation_id,
                   type=args.card_type, latest=args.selector == "latest", id=args.card_id))
        return result.mappings().first()

    async def current_email(self, session, context, email_id):
        result = await session.execute(text("""
            SELECT id,source_version FROM email_messages
            WHERE id=:id AND tenant_id=:tenant AND user_id=:user
        """), dict(id=email_id, tenant=context.tenant_id, user=context.user_id))
        return result.mappings().first()
