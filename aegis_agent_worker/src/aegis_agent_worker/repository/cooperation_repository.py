"""协同交接的SQL事实；不读正文、不让模型控制身份或连接。"""
from sqlalchemy import text
import json
from aegis_agent_worker.harness.cooperation_scope import step_tags


class CooperationRepository:
    async def execution_status(self,session,run):
        result=await session.execute(text('SELECT status FROM agent_runs WHERE id=:run AND tenant_id=:tenant AND user_id=:user'),
            dict(run=run.id,tenant=run.tenant_id,user=run.user_id))
        row=result.mappings().first()
        return row['status'] if row else None

    async def draft_valid(self,session,run,result):
        expected={'waiting_confirmation':'pending','completed':'executed','rejected':'rejected'}[result.status]
        rows=await session.execute(text('''
            SELECT ai.id FROM calendar_event_drafts d JOIN approval_items ai ON ai.resource_id=d.id
            WHERE d.id=:draft AND d.run_id=:run AND d.tenant_id=:tenant AND d.user_id=:user
              AND ai.id=:approval AND ai.tenant_id=:tenant AND ai.user_id=:user AND ai.run_id=:run
              AND ai.resource_type='calendar_event_draft' AND ai.action='calendar.create_event'
              AND ai.draft_version=d.version AND ai.status=:status
              AND (:status!='executed' OR d.status='executed')
        '''),dict(draft=result.draft_id,approval=result.approval_item_id,run=run.id,
                  tenant=run.tenant_id,user=run.user_id,status=expected))
        return rows.mappings().first() is not None

    async def step_usage(self,session,run,step_id,plan_digest):
        rows=await session.execute(text('''
            SELECT count(*) FILTER (WHERE kind='model') AS model_calls,
                   count(*) FILTER (WHERE kind='tool') AS tool_calls,
                   CASE WHEN count(*) FILTER (WHERE kind='model')>0
                         AND count(*) FILTER (WHERE kind='model')=count(total_tokens) FILTER (WHERE kind='model')
                        THEN sum(total_tokens) FILTER (WHERE kind='model') ELSE NULL END AS total_tokens
            FROM run_execution_attempts
            WHERE run_id=:run AND tenant_id=:tenant AND user_id=:user
              AND risk_flags @> CAST(:tags AS jsonb)
        '''),dict(run=run.id,tenant=run.tenant_id,user=run.user_id,tags=json.dumps(step_tags(step_id,plan_digest))))
        return dict(rows.mappings().one())

    async def email_source(self, session, run, reference):
        result = await session.execute(text('''
            SELECT e.id,e.source_version,e.connection_id,e.provider_message_id
            FROM email_messages e WHERE e.id=:id AND e.tenant_id=:tenant AND e.user_id=:user
              AND e.source_version=:version
        '''), dict(id=reference.id,tenant=run.tenant_id,user=run.user_id,version=reference.source_version))
        row = result.mappings().first()
        return dict(row) if row else None
