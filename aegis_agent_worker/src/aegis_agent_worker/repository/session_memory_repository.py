"""会话工作记忆的授权快照；单条SQL同一快照，仅取结构字段，不读取业务正文。"""
from sqlalchemy import text


class SessionMemoryRepository:
    async def snapshot(self, session, run, policy):
        result = await session.execute(text("""
            SELECT c.id, (extract(epoch FROM statement_timestamp())*1000000)::bigint AS snapshot_tick,
              COALESCE((SELECT max(cm.sequence_no) FROM conversation_messages cm
                WHERE cm.conversation_id=c.id AND cm.tenant_id=:tenant),0) AS sequence_no,
              COALESCE((SELECT jsonb_agg(to_jsonb(t) ORDER BY t.created_at DESC,t.id DESC)
                FROM (SELECT ar.id,ar.run_type,ar.status,ar.updated_at,ar.created_at FROM agent_runs ar
                  WHERE ar.conversation_id=c.id AND ar.tenant_id=:tenant AND ar.user_id=:user
                    AND ar.id<>:run ORDER BY ar.created_at DESC,ar.id DESC LIMIT :tasks) t),'[]'::jsonb) AS tasks,
              COALESCE((SELECT jsonb_agg(to_jsonb(a) ORDER BY a.created_at DESC,a.id DESC)
                FROM (SELECT ca.id,ca.run_id,ca.artifact_type,ca.created_at,ca.expires_at,
                    md5(ca.payload::text) AS source_version FROM conversation_message_artifacts ca
                  WHERE ca.conversation_id=c.id AND ca.tenant_id=:tenant AND ca.user_id=:user
                    AND ca.expires_at>now()
                  ORDER BY ca.created_at DESC,ca.id DESC LIMIT :cards) a),'[]'::jsonb) AS cards
            FROM conversations c WHERE c.id=:conversation AND c.tenant_id=:tenant AND c.user_id=:user
              AND c.status='active'
              AND EXISTS (SELECT 1 FROM agent_runs own WHERE own.id=:run AND own.tenant_id=:tenant
                AND own.user_id=:user AND own.conversation_id=c.id AND own.run_type='conversation')
        """),dict(tenant=run.tenant_id,user=run.user_id,run=run.id,conversation=run.conversation_id,
                   tasks=policy.max_tasks,cards=policy.max_cards))
        row = result.mappings().first()
        return dict(row) if row else None
