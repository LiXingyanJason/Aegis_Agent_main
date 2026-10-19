"""Agent 使用长期记忆时的数据访问。"""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class MemoryRepository:
    """仅暴露读取非敏感记忆和记录实际使用情况的最小能力。"""

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
