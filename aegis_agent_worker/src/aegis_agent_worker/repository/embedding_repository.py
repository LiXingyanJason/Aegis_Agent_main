"""Embedding 原子预算和审计 SQL；真实主体及外发权限在数据库侧核验。"""
from sqlalchemy import text
from aegis_agent_worker.harness.exceptions import HarnessError


class EmbeddingRepository:
    async def authorize(self, session, subject, profile):
        params = dict(id=subject.id, tenant=subject.tenant_id, user=subject.user_id)
        if subject.kind == "run":
            sql = "SELECT id FROM agent_runs WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND status='running'"
        else:
            sql = """SELECT j.id FROM knowledge_index_jobs j
              JOIN knowledge_documents d ON d.id=j.document_id AND d.tenant_id=j.tenant_id
              JOIN knowledge_document_versions v ON v.id=j.version_id AND v.document_id=d.id AND v.tenant_id=d.tenant_id
              WHERE j.id=:id AND j.tenant_id=:tenant AND j.user_id=:user AND d.user_id=:user
                AND d.tenant_id=:tenant AND d.deleted_at IS NULL AND NOT d.is_sensitive
                AND d.external_embedding_allowed AND j.status IN ('queued','running')
                AND v.status IN ('pending','indexing') AND v.embedding_provider=:provider
                AND v.embedding_model=:model AND v.embedding_model_version=:version AND v.embedding_dimensions=:dimensions"""
            params.update(provider=profile.provider, model=profile.model, version=profile.version, dimensions=profile.dimensions)
        if (await session.execute(text(sql), params)).first() is None:
            raise HarnessError("EMBEDDING_SUBJECT_DENIED", "任务不可访问、模型不匹配或资料未获外发授权")

    async def ensure_budget(self, session, subject, profile, default_version):
        await session.execute(text("""INSERT INTO embedding_execution_budgets
          (tenant_id,user_id,run_id,index_job_id,policy_version,model_provider,model_name,model_version,dimensions)
          VALUES(:tenant,:user,:run,:job,:policy,:provider,:model,:version,:dimensions) ON CONFLICT DO NOTHING"""),
          dict(tenant=subject.tenant_id, user=subject.user_id,
               run=subject.id if subject.kind == "run" else None, job=subject.id if subject.kind == "job" else None,
               policy=default_version, provider=profile.provider, model=profile.model,
               version=profile.version, dimensions=profile.dimensions))
        # 列名仅来自代码，恢复任务固定原策略和模型，不能重置预算或混用向量空间。
        field = "run_id" if subject.kind == "run" else "index_job_id"
        row = (await session.execute(text(f"""SELECT * FROM embedding_execution_budgets
          WHERE {field}=:id AND tenant_id=:tenant AND user_id=:user FOR UPDATE"""),
          dict(id=subject.id, tenant=subject.tenant_id, user=subject.user_id))).mappings().one()
        if (row["model_provider"], row["model_name"], row["model_version"], row["dimensions"]) != (
                profile.provider, profile.model, profile.version, profile.dimensions):
            raise HarnessError("EMBEDDING_PROFILE_MISMATCH", "任务绑定的 Embedding 模型或维度已改变")
        return row

    async def reserve(self, session, budget_id, subject, policy, chars):
        row = (await session.execute(text("""UPDATE embedding_execution_budgets
          SET call_count=call_count+1,input_chars=input_chars+:chars
          WHERE id=:id AND tenant_id=:tenant AND user_id=:user AND policy_version=:policy
            AND call_count<:calls AND input_chars+:chars<=:maximum RETURNING id"""),
          dict(id=budget_id, tenant=subject.tenant_id, user=subject.user_id, policy=policy.version,
               chars=chars, calls=policy.max_calls, maximum=policy.max_total_input_chars))).first()
        if row is None:
            raise HarnessError("EMBEDDING_BUDGET_EXCEEDED", "Embedding 调用或字符预算已耗尽")

    async def start(self, session, budget_id, subject, chars, batch_items):
        return (await session.execute(text("""INSERT INTO embedding_execution_attempts
          (budget_id,tenant_id,user_id,purpose,input_chars,batch_items)
          VALUES(:budget,:tenant,:user,:purpose,:chars,:items) RETURNING id"""),
          dict(budget=budget_id, tenant=subject.tenant_id, user=subject.user_id,
               purpose=subject.purpose, chars=chars, items=batch_items))).scalar_one()

    async def finish(self, session, attempt_id, subject, *, status, code, duration_ms, usage):
        await session.execute(text("""UPDATE embedding_execution_attempts SET status=:status,error_code=:code,
          duration_ms=:duration,finished_at=now(),prompt_tokens=:prompt,total_tokens=:total
          WHERE id=:id AND tenant_id=:tenant AND user_id=:user"""),
          dict(status=status, code=code, duration=duration_ms, prompt=usage.prompt_tokens,
               total=usage.total_tokens, id=attempt_id, tenant=subject.tenant_id, user=subject.user_id))
