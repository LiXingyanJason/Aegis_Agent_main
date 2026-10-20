"""已归属任务查询内的最新安全进度投影；不读取模型/工具原始载荷。"""
LATEST_COOPERATION_PROGRESS = """
    (SELECT e.payload || jsonb_build_object('event_no',e.event_no,'occurred_at',e.created_at)
     FROM run_events e
     WHERE e.run_id=agent_runs.id AND e.tenant_id=agent_runs.tenant_id
       AND e.event_type='cooperation_progress'
     ORDER BY e.event_no DESC LIMIT 1) AS cooperation
"""
