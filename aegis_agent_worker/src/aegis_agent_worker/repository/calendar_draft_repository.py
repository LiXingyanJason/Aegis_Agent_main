"""日历草稿/确认项SQL；模型推理、控制流及业务校验不在Repository。"""
import json
from hashlib import sha256

from sqlalchemy import text


async def create_calendar_draft(session, *, run, connection_id, title, start_at, end_at,
        attendees, description, timezone="Asia/Shanghai", draft_id=None, status="pending_approval"):
    if status not in {"draft", "pending_approval"}:
        raise ValueError("新草稿状态无效")
    result = await session.execute(text("""
        INSERT INTO calendar_event_drafts
          (id,tenant_id,user_id,run_id,connection_id,calendar_external_id,calendar_name,
           title,description,start_at,end_at,timezone,status)
        VALUES (COALESCE(CAST(:draft_id AS uuid),gen_random_uuid()),:tenant_id,:user_id,:run_id,:connection_id,
                'primary','主日历',:title,:description,:start_at,:end_at,:timezone,:status) RETURNING id
    """), dict(draft_id=draft_id,tenant_id=run.tenant_id,user_id=run.user_id,run_id=run.id,
        connection_id=connection_id,title=title[:500],description=description[:4000],
        start_at=start_at,end_at=end_at,timezone=timezone,status=status))
    draft_id = result.mappings().one()["id"]
    for email in sorted(set(attendees)):
        await session.execute(text("""
            INSERT INTO calendar_draft_attendees(tenant_id,draft_id,email)
            VALUES(:tenant_id,:draft_id,:email) ON CONFLICT(draft_id,email) DO NOTHING
        """), dict(tenant_id=run.tenant_id,draft_id=draft_id,email=email))
    return draft_id


async def create_calendar_approval(session, *, run, draft_id, title, start_at, end_at, attendees,
        recommendation_preview, timezone="Asia/Shanghai", version=1):
    preview = dict(title=title,start_at=start_at.isoformat(),end_at=end_at.isoformat(),
                   attendees=attendees,timezone=timezone,recommendation=recommendation_preview)
    digest = sha256(json.dumps(preview, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    result = await session.execute(text("""
        INSERT INTO approval_items(tenant_id,user_id,run_id,action,risk_level,resource_type,resource_id,
            title,preview_snapshot,parameters_hash,draft_version,expires_at)
        VALUES(:tenant_id,:user_id,:run_id,'calendar.create_event','write','calendar_event_draft',:draft_id,
            :title,CAST(:preview AS jsonb),:parameters_hash,:version,now()+interval '24 hours') RETURNING id
    """), dict(tenant_id=run.tenant_id,user_id=run.user_id,run_id=run.id,draft_id=draft_id,
        title=f"创建日历事件：{title}"[:256],preview=json.dumps(preview,ensure_ascii=False),
        parameters_hash=digest,version=version))
    return result.mappings().one()["id"]


class CalendarDraftRepository:
    async def completed_execution(self, session, run, draft_id, approval_id):
        result = await session.execute(text("""
            SELECT e.provider_resource_id FROM external_action_executions e
            JOIN approval_items ai ON ai.id=e.approval_item_id AND ai.tenant_id=e.tenant_id
            JOIN calendar_event_drafts d ON d.id=ai.resource_id AND d.tenant_id=ai.tenant_id AND d.user_id=ai.user_id
            WHERE ai.id=:approval_id AND d.id=:draft_id AND ai.run_id=:run_id AND d.run_id=:run_id
              AND ai.tenant_id=:tenant_id AND ai.user_id=:user_id AND ai.draft_version=d.version
              AND ai.resource_type='calendar_event_draft' AND ai.action='calendar.create_event'
              AND ai.status='executed' AND d.status='executed' AND e.status='succeeded'
              AND EXISTS(SELECT 1 FROM approval_decisions ad WHERE ad.approval_item_id=ai.id
                AND ad.tenant_id=ai.tenant_id AND ad.actor_user_id=ai.user_id AND ad.decision='approved' AND ad.checked=true)
            LIMIT 1
        """),dict(approval_id=approval_id,draft_id=draft_id,run_id=run.id,tenant_id=run.tenant_id,user_id=run.user_id))
        row=result.mappings().first()
        return row["provider_resource_id"] if row and row["provider_resource_id"] else None

    async def has_execution(self, session, run, approval_id):
        result=await session.execute(text("""
            SELECT id FROM external_action_executions WHERE approval_item_id=:approval_id AND tenant_id=:tenant_id LIMIT 1
        """),dict(approval_id=approval_id,tenant_id=run.tenant_id))
        return result.mappings().first() is not None

    async def insert(self, session, **kwargs):
        return await create_calendar_draft(session, **kwargs)

    async def create_approval(self, session, **kwargs):
        return await create_calendar_approval(session, **kwargs)

    async def connection(self, session, run, connection_id):
        from aegis_agent_worker.repository.tool_repository import ProviderConnectionRepository
        return await ProviderConnectionRepository().find_active_connection(session,
            tenant_id=run.tenant_id,user_id=run.user_id,providers=("google_calendar","outlook_calendar"),
            required_scope="calendar.read",required_connection_id=connection_id)

    async def lock_run(self, session, run):
        row = await session.execute(text("""
            SELECT id FROM agent_runs WHERE id=:run_id AND tenant_id=:tenant_id AND user_id=:user_id
              AND status IN ('running','waiting_confirmation') FOR UPDATE
        """), dict(run_id=run.id,tenant_id=run.tenant_id,user_id=run.user_id))
        return row.mappings().first() is not None

    async def load(self, session, run, draft_id):
        result = await session.execute(text("""
            SELECT d.*, pc.status='active' AND (pc.expires_at IS NULL OR pc.expires_at>now())
                     AND pc.scopes @> '["calendar.read"]'::jsonb AS readable,
                   pc.status='active' AND (pc.expires_at IS NULL OR pc.expires_at>now())
                     AND pc.scopes @> '["calendar.write"]'::jsonb AS writable,
                   meta.payload AS metadata
            FROM calendar_event_drafts d
            JOIN provider_connections pc ON pc.id=d.connection_id AND pc.tenant_id=d.tenant_id AND pc.user_id=d.user_id
              AND pc.provider IN ('google_calendar','outlook_calendar')
            LEFT JOIN LATERAL (
                SELECT payload FROM run_events WHERE run_id=d.run_id AND tenant_id=d.tenant_id
                  AND event_type='cooperation_calendar_draft_saved' AND payload->>'draft_id'=CAST(d.id AS text)
                ORDER BY event_no DESC LIMIT 1
            ) meta ON true
            WHERE d.id=:draft_id AND d.run_id=:run_id AND d.tenant_id=:tenant_id AND d.user_id=:user_id
        """),dict(draft_id=draft_id,run_id=run.id,tenant_id=run.tenant_id,user_id=run.user_id))
        row = result.mappings().first()
        if row is None:
            return None
        value = dict(row)
        if isinstance(value.get("metadata"), str):
            value["metadata"] = json.loads(value["metadata"])
        attendees = await session.execute(text("""
            SELECT email FROM calendar_draft_attendees WHERE tenant_id=:tenant_id AND draft_id=:draft_id ORDER BY email
        """),dict(tenant_id=run.tenant_id,draft_id=draft_id))
        value["attendees"] = [row["email"] for row in attendees.mappings().all()]
        return value

    async def approval(self, session, run, draft_id, version):
        result = await session.execute(text("""
            SELECT *, expires_at>now() AS valid FROM approval_items
            WHERE resource_id=:draft_id AND resource_type='calendar_event_draft' AND action='calendar.create_event'
              AND run_id=:run_id AND tenant_id=:tenant_id AND user_id=:user_id AND draft_version=:version
            ORDER BY created_at DESC LIMIT 1
        """), dict(draft_id=draft_id,run_id=run.id,tenant_id=run.tenant_id,user_id=run.user_id,version=version))
        row = result.mappings().first()
        return dict(row) if row else None

    async def mark_pending(self, session, run, draft_id, version):
        await session.execute(text("""
            UPDATE calendar_event_drafts SET status='pending_approval',updated_at=now()
            WHERE id=:draft_id AND tenant_id=:tenant_id AND user_id=:user_id AND run_id=:run_id
              AND version=:version AND status='draft'
        """),dict(draft_id=draft_id,tenant_id=run.tenant_id,user_id=run.user_id,run_id=run.id,version=version))

    async def mark_ready(self, session, run, approval_id):
        result = await session.execute(text("""
            UPDATE agent_runs ar SET status='waiting_confirmation',current_stage='等待用户确认会议安排',updated_at=now()
            WHERE ar.id=:run_id AND ar.tenant_id=:tenant_id AND ar.user_id=:user_id
              AND ar.status IN ('running','waiting_confirmation')
              AND EXISTS (
                SELECT 1 FROM approval_items ai JOIN calendar_event_drafts d ON d.id=ai.resource_id
                WHERE ai.id=:approval_id AND ai.run_id=ar.id AND ai.tenant_id=ar.tenant_id AND ai.user_id=ar.user_id
                  AND ai.status='pending' AND ai.expires_at>now() AND ai.draft_version=d.version
                  AND d.status='pending_approval' AND d.tenant_id=ar.tenant_id AND d.user_id=ar.user_id AND d.run_id=ar.id)
            RETURNING ar.id
        """),dict(run_id=run.id,tenant_id=run.tenant_id,user_id=run.user_id,approval_id=approval_id))
        return result.mappings().first() is not None
