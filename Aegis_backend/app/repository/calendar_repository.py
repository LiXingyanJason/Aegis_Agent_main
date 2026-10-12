"""日历来源、站内草稿和创建来源映射的数据访问。"""

from sqlalchemy import text


class CalendarRepository:
    async def connections(self, session, user, connection_id=None):
        """只查询当前用户的日历连接，包括供业务层判定的失效连接。"""
        result = await session.execute(text("""
            SELECT id, provider, scopes, status, expires_at
            FROM provider_connections
            WHERE tenant_id = :tenant_id AND user_id = :user_id
              AND provider IN ('google_calendar', 'outlook_calendar')
              AND (CAST(:connection_id AS uuid) IS NULL OR id = CAST(:connection_id AS uuid))
            ORDER BY created_at, id
        """), {"tenant_id": user.tenant_id, "user_id": user.id, "connection_id": connection_id})
        return [dict(row) for row in result.mappings().all()]

    async def drafts(self, session, user, *, param=None, draft_id=None):
        """关联每份草稿当前版本最新的确认项，并始终限制租户与用户。"""
        filters = "AND d.id = :draft_id" if draft_id else """
            AND d.start_at < :end_at AND d.end_at > :start_at
            AND (CAST(:connection_id AS uuid) IS NULL OR d.connection_id = CAST(:connection_id AS uuid))
            AND (:approval_status = 'all' OR ai.status = :approval_status)
        """
        args = {"tenant_id": user.tenant_id, "user_id": user.id}
        if draft_id:
            args["draft_id"] = draft_id
        else:
            args.update(start_at=param.start_at, end_at=param.end_at,
                        connection_id=param.connection_id, approval_status=param.approval_status)
        result = await session.execute(text("""
            SELECT d.id AS draft_id, d.run_id, d.connection_id,
                   d.calendar_external_id AS calendar_id, d.title, d.description,
                   d.start_at, d.end_at, d.timezone, d.version, d.status AS draft_status,
                   ai.id AS approval_item_id, ai.status AS approval_status, ai.expires_at,
                   ai.preview_snapshot,
                   COALESCE(ai.status = 'pending' AND ai.expires_at > now()
                       AND ar.status = 'waiting_confirmation'
                       AND d.status = 'pending_approval', false) AS can_confirm,
                   ARRAY(SELECT a.email FROM calendar_draft_attendees a
                         WHERE a.draft_id = d.id AND a.tenant_id = d.tenant_id
                         ORDER BY a.email) AS attendees
            FROM calendar_event_drafts d
            JOIN agent_runs ar ON ar.id = d.run_id AND ar.tenant_id = d.tenant_id
                              AND ar.user_id = d.user_id
            LEFT JOIN LATERAL (
                SELECT p.* FROM approval_items p
                WHERE p.resource_id = d.id AND p.resource_type = 'calendar_event_draft'
                  AND p.action = 'calendar.create_event' AND p.tenant_id = d.tenant_id
                  AND p.user_id = d.user_id AND p.draft_version = d.version
                ORDER BY p.created_at DESC, p.id DESC LIMIT 1
            ) ai ON true
            WHERE d.tenant_id = :tenant_id AND d.user_id = :user_id
        """ + filters + " ORDER BY d.start_at, d.end_at, d.id"), args)
        return [dict(row) for row in result.mappings().all()]

    async def event_origin(self, session, user, connection_id, calendar_id, external_event_id):
        """只从当前用户成功的外部写入记录映射回原始草稿。"""
        result = await session.execute(text("""
            SELECT d.id AS origin_draft_id, d.run_id AS origin_run_id
            FROM external_action_executions e
            JOIN approval_items ai ON ai.id = e.approval_item_id AND ai.tenant_id = e.tenant_id
            JOIN calendar_event_drafts d ON d.id = ai.resource_id AND d.tenant_id = ai.tenant_id
            WHERE e.tenant_id = :tenant_id AND ai.user_id = :user_id AND d.user_id = :user_id
              AND e.connection_id = :connection_id AND d.connection_id = :connection_id
              AND d.calendar_external_id = :calendar_id
              AND ai.resource_type = 'calendar_event_draft' AND ai.action = 'calendar.create_event'
              AND e.status = 'succeeded' AND e.provider_resource_id = :external_event_id
            ORDER BY e.finished_at DESC LIMIT 1
        """), dict(tenant_id=user.tenant_id, user_id=user.id, connection_id=connection_id,
                   calendar_id=calendar_id, external_event_id=external_event_id))
        row = result.mappings().first()
        return dict(row) if row else {}
