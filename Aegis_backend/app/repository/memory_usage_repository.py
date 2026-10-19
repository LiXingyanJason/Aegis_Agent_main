"""任务上下文的安全元数据查询；绝不读取display_summary或记忆正文。"""
import json
from sqlalchemy import text


class MemoryUsageRepository:
    async def attempts(self,session,user,run_id,limit):
        rows=await session.execute(text("""SELECT id,purpose,status,started_at,memory_count,memory_policy_version,
          memory_selection_reason,memory_selection_details,prompt_tokens,completion_tokens,total_tokens
          FROM run_execution_attempts WHERE run_id=:run AND tenant_id=:tenant AND user_id=:user AND kind='model'
          ORDER BY started_at DESC,id DESC LIMIT :limit"""),
          dict(run=run_id,tenant=user.tenant_id,user=user.id,limit=limit+1))
        return [dict(row) for row in rows.mappings().all()]

    async def usages(self,session,user,run_id,attempt_ids,limit,*,legacy=False):
        # 页内attempt最多100，每次最多50条记忆；历史无attempt最多limit条，不伪造调用关联。
        condition='u.attempt_id IS NULL' if legacy else "u.attempt_id IN (SELECT value::uuid FROM jsonb_array_elements_text(CAST(:attempts AS jsonb)))"
        rows=await session.execute(text("""SELECT * FROM (
          SELECT u.attempt_id,u.memory_id,u.memory_version,u.memory_type,u.source_kind,u.source_object_id,u.source_version,
            u.included_in_prompt,u.selection_reason,
            CASE WHEN m.id IS NULL THEN 'restricted' WHEN m.deleted_at IS NOT NULL THEN 'deleted'
              WHEN m.is_sensitive OR m.sensitivity<>'normal' THEN 'restricted'
              WHEN m.status<>'confirmed' OR m.confirmed_at IS NULL OR m.expires_at<=now() OR
                NOT aegis_memory_source_accessible(m.tenant_id,m.user_id,m.source_kind,m.source_object_id,m.source_version) THEN 'inactive'
              WHEN u.memory_version IS NULL THEN 'unknown' WHEN u.memory_version<>m.version THEN 'changed'
              WHEN u.source_kind IS DISTINCT FROM m.source_kind OR (u.source_kind<>'manual' AND
                (u.source_object_id IS DISTINCT FROM m.source_object_id OR u.source_version IS DISTINCT FROM m.source_version)) THEN 'changed'
              ELSE 'available' END AS current_status,
            row_number() OVER(PARTITION BY u.attempt_id ORDER BY u.created_at,u.id) AS position
          FROM run_memory_usages u JOIN agent_runs ar ON ar.id=u.run_id AND ar.tenant_id=u.tenant_id
          LEFT JOIN memory_items m ON m.id=u.memory_id AND m.tenant_id=:tenant AND m.user_id=:user
          WHERE u.run_id=:run AND u.tenant_id=:tenant AND ar.user_id=:user AND """+condition+"""
          ) safe WHERE position<=:limit ORDER BY attempt_id NULLS LAST,position"""),
          dict(run=run_id,tenant=user.tenant_id,user=user.id,attempts=json.dumps([str(item) for item in attempt_ids]),limit=limit))
        return [dict(row) for row in rows.mappings().all()]
