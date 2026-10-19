"""Agent 使用长期记忆时的数据访问。"""

from typing import Any
from uuid import UUID
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class MemoryRepository:
    """仅暴露读取非敏感记忆和记录实际使用情况的最小能力。"""

    async def recall_candidates(self,session,*,tenant_id,user_id,hints,policy):
        """权限前置；SQL按相关档位排序后才LIMIT，不先截取最近记录。"""
        await session.execute(text("SELECT set_config('statement_timeout',:timeout,true)"),
                              {'timeout':str(policy.recall_timeout_seconds*1000)})
        result=await session.execute(text("""
          WITH eligible AS MATERIALIZED (
            SELECT id,content,source,updated_at,memory_type,entity_value,version,
                   source_kind,source_object_id,source_version,entity_key
            FROM memory_items WHERE tenant_id=:tenant_id AND user_id=:user_id
              AND deleted_at IS NULL AND is_sensitive=false AND sensitivity='normal'
              AND status='confirmed' AND confirmed_at IS NOT NULL AND (expires_at IS NULL OR expires_at>now())
              AND aegis_memory_source_accessible(tenant_id,user_id,source_kind,source_object_id,source_version)
              AND memory_type IN (SELECT jsonb_array_elements_text(CAST(:types AS jsonb)))
          ), scored AS (
            SELECT e.*,
              (SELECT count(*) FROM jsonb_array_elements_text(CAST(:terms AS jsonb)) t(value)
                WHERE strpos(lower(e.content),t.value)>0) AS matches,
              EXISTS(SELECT 1 FROM jsonb_array_elements_text(CAST(:affinity AS jsonb)) t(value)
                WHERE strpos(lower(e.content),t.value)>0) AS affinity,
              CASE WHEN memory_type='entity' THEN entity_key
                WHEN content ~* '时区|北京时间|Asia/|UTC' THEN 'timezone'
                WHEN content ~ '语气|措辞|风格|正式|简洁|友好|礼貌' THEN 'reply_style'
                WHEN content ~ '会议|开会|安排|空闲' THEN 'meeting_preferences'
                WHEN content ~* '联系人|邮箱|email' THEN 'contact' ELSE 'unknown' END AS topic
            FROM eligible e
          ), matched AS MATERIALIZED (
            SELECT *,CASE WHEN memory_type='entity' THEN 3 WHEN matches>=:strong THEN 2 ELSE 1 END AS match_tier
            FROM scored WHERE (memory_type='long_term_summary' AND (matches>0 OR affinity)) OR
              (memory_type='entity' AND entity_key IN (SELECT jsonb_array_elements_text(CAST(:keys AS jsonb)))
                AND (entity_key<>'contact' OR
                  (length(entity_value->>'name')>0 AND strpos(:task,lower(entity_value->>'name'))>0) OR
                  (length(entity_value->>'email')>0 AND strpos(:task,lower(entity_value->>'email'))>0)))
          ), topics AS (
            SELECT topic,count(DISTINCT lower(btrim(content))) FILTER(WHERE memory_type='long_term_summary')>1 AS summary_conflicted,
              bool_or(memory_type='entity') AS has_entity_topic FROM matched GROUP BY topic
          )
          SELECT m.*,t.summary_conflicted,t.has_entity_topic,
            EXISTS(SELECT 1 FROM matched WHERE memory_type='entity') AS has_any_entity
          FROM matched m JOIN topics t USING(topic)
          ORDER BY CASE WHEN m.memory_type='long_term_summary' AND (t.summary_conflicted OR t.has_entity_topic OR
            (m.topic='unknown' AND EXISTS(SELECT 1 FROM matched WHERE memory_type='entity'))) THEN 1 ELSE 0 END,
            m.match_tier DESC,CASE WHEN m.memory_type='entity' THEN 0 ELSE m.matches END DESC,
            m.updated_at DESC,m.id DESC LIMIT :limit
        """),dict(tenant_id=tenant_id,user_id=user_id,types=json.dumps(hints['types']),terms=json.dumps(hints['terms']),
            affinity=json.dumps(hints['affinity']),keys=json.dumps(hints['entity_keys']),task=hints['task'],
            strong=policy.strong_term_matches,limit=policy.candidate_limit+1))
        return [dict(row) for row in result.mappings().all()]

    async def list_active_non_sensitive(
        self, session: AsyncSession, *, tenant_id: UUID, user_id: UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        """读取当前用户未删除、非敏感的候选记忆，绝不查询敏感正文。"""
        result = await session.execute(
            text(
                "SELECT id, content, source, updated_at, memory_type,entity_value,version, "
                "source_kind,source_object_id,source_version FROM memory_items "
                "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                "AND deleted_at IS NULL AND is_sensitive=false "
                "AND sensitivity='normal' AND status='confirmed' AND confirmed_at IS NOT NULL "
                "AND (expires_at IS NULL OR expires_at>now()) "
                "AND aegis_memory_source_accessible(tenant_id,user_id,source_kind,source_object_id,source_version) "
                "ORDER BY updated_at DESC, id DESC LIMIT :limit"
            ),
            {"tenant_id": tenant_id, "user_id": user_id, "limit": limit},
        )
        return [dict(row) for row in result.mappings().all()]

    async def record_bundle_usage(self, session, *, run, attempt_id, bundle):
        for item in bundle.items:
            result = await session.execute(text("""
                INSERT INTO run_memory_usages(tenant_id,run_id,memory_id,usage_purpose,included_in_prompt,
                    attempt_id,model_purpose,memory_version,memory_type,source_kind,source_object_id,
                    source_version,selection_reason,policy_version,excerpt_bytes,estimated_tokens)
                SELECT :tenant,:run,:memory,'context',true,a.id,:purpose,:version,:type,:source_kind,
                    :source_object,:source_version,:reason,:policy,:bytes,:estimated
                FROM run_execution_attempts a WHERE a.id=:attempt AND a.run_id=:run
                    AND a.tenant_id=:tenant AND a.user_id=:user AND a.kind='model'
                    AND EXISTS(SELECT 1 FROM memory_items m WHERE m.id=:memory AND m.version=:version
                        AND m.tenant_id=:tenant AND m.user_id=:user AND m.deleted_at IS NULL
                        AND m.status='confirmed' AND m.confirmed_at IS NOT NULL
                        AND m.sensitivity='normal' AND NOT m.is_sensitive
                        AND (m.expires_at IS NULL OR m.expires_at>now())
                        AND aegis_memory_source_accessible(m.tenant_id,m.user_id,m.source_kind,m.source_object_id,m.source_version))
                RETURNING id
            """), dict(tenant=run.tenant_id, user=run.user_id, run=run.id, memory=item.memory_id,
                attempt=attempt_id, purpose=bundle.purpose, version=item.version, type=item.memory_type,
                source_kind=item.source.kind, source_object=item.source.object_id, source_version=item.source.version,
                reason=item.selection_reason, policy=bundle.policy_version, bytes=len(item.excerpt.encode('utf-8')),
                estimated=None))
            # 版本/来源在提交审计时再次核验；若变化，不带旧摘录向供应商发送。
            result.scalar_one()

    async def finish_bundle_usage(self, session, *, run, attempt_id, usage):
        await session.execute(text("""
            UPDATE run_memory_usages SET call_prompt_tokens=:prompt,call_completion_tokens=:completion,
                call_total_tokens=:total WHERE attempt_id=:attempt AND tenant_id=:tenant AND run_id=:run
        """), dict(prompt=usage.prompt_tokens, completion=usage.completion_tokens,total=usage.total_tokens,
                   attempt=attempt_id,tenant=run.tenant_id,run=run.id))
