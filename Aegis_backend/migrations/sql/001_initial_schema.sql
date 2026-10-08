
-- 执行者须具有 CREATE EXTENSION、CREATE TABLE、CREATE FUNCTION 权限。
-- 应用事务中应执行：SET LOCAL app.tenant_id = '...'; SET LOCAL app.user_id = '...';

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION aegis_set_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END; $$;

CREATE OR REPLACE FUNCTION aegis_current_tenant_id()
RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION aegis_current_user_id()
RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.user_id', true), '')::uuid
$$;

CREATE TABLE tenants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name varchar(128) NOT NULL UNIQUE,
  status varchar(32) NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  oidc_issuer varchar(512) NOT NULL,
  oidc_subject varchar(512) NOT NULL,
  email varchar(320) NOT NULL,
  display_name varchar(128) NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 128),
  timezone varchar(64) NOT NULL DEFAULT 'Asia/Shanghai',
  status varchar(32) NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (oidc_issuer, oidc_subject)
);
CREATE INDEX ix_users_tenant_email ON users (tenant_id, email);

CREATE TABLE provider_connections (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  user_id uuid NOT NULL REFERENCES users(id),
  provider varchar(32) NOT NULL CHECK (provider IN ('google_calendar','outlook_calendar','gmail','outlook_mail')),
  provider_account_id varchar(255) NOT NULL,
  credential_ref varchar(512) NOT NULL,
  scopes jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(scopes) = 'array'),
  status varchar(32) NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','revoked','error')),
  expires_at timestamptz,
  last_verified_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, user_id, provider, provider_account_id)
);
CREATE INDEX ix_provider_connections_owner ON provider_connections (tenant_id, user_id, status);

CREATE TABLE conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  user_id uuid NOT NULL REFERENCES users(id),
  title varchar(200) NOT NULL DEFAULT '新对话',
  status varchar(32) NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  last_message_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_conversations_list ON conversations (tenant_id, user_id, last_message_at DESC);

-- 与 agent_runs 存在可选双向引用，run_id 外键在 agent_runs 建立后补充。
CREATE TABLE conversation_messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  run_id uuid,
  role varchar(16) NOT NULL CHECK (role IN ('user','assistant','system','tool')),
  content text NOT NULL CHECK (length(content) <= 65536),
  display_content text NOT NULL CHECK (length(display_content) <= 65536),
  sequence_no integer NOT NULL CHECK (sequence_no > 0),
  is_final boolean NOT NULL DEFAULT true,
  client_message_id varchar(128),
  content_hash char(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  tool_call_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (conversation_id, sequence_no),
  -- 仅用户消息具有 client_message_id；NULL 的 Agent/系统/工具消息必须允许重复。
  UNIQUE (conversation_id, client_message_id),
  CHECK ((role = 'user' AND client_message_id IS NOT NULL) OR role <> 'user')
);
CREATE INDEX ix_messages_conversation ON conversation_messages (tenant_id, conversation_id, sequence_no);

CREATE TABLE agent_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  user_id uuid NOT NULL REFERENCES users(id),
  -- 邮件摘要/回复起草等后台任务不属于一个对话；普通会话任务仍写入此字段。
  conversation_id uuid REFERENCES conversations(id),
  input_message_id uuid REFERENCES conversation_messages(id),
  parent_run_id uuid REFERENCES agent_runs(id),
  run_type varchar(32) NOT NULL CHECK (run_type IN ('conversation','mail_extraction','mail_reply_draft')),
  intent varchar(64),
  status varchar(32) NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','waiting_confirmation','completed','failed','cancelled')),
  current_stage varchar(64),
  model_provider varchar(64),
  model_name varchar(128),
  request_id varchar(64) NOT NULL UNIQUE,
  trace_id varchar(64),
  result_summary text,
  error_code varchar(64),
  error_message varchar(1000),
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at)
);
CREATE INDEX ix_runs_owner_created ON agent_runs (tenant_id, user_id, created_at DESC);
CREATE INDEX ix_runs_trace ON agent_runs (trace_id) WHERE trace_id IS NOT NULL;
ALTER TABLE conversation_messages
  ADD CONSTRAINT fk_message_run FOREIGN KEY (run_id) REFERENCES agent_runs(id);

CREATE TABLE run_steps (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  sequence_no integer NOT NULL CHECK (sequence_no > 0),
  step_key varchar(64) NOT NULL,
  label varchar(256) NOT NULL,
  status varchar(32) NOT NULL CHECK (status IN ('pending','running','succeeded','failed','skipped')),
  detail varchar(1000),
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, sequence_no), UNIQUE (run_id, step_key),
  CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at)
);

-- 分区表的 PK 必须包含分区键；id 仍由全局 identity 序列生成。
CREATE TABLE run_events (
  id bigint GENERATED ALWAYS AS IDENTITY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  event_no bigint NOT NULL CHECK (event_no > 0),
  event_type varchar(64) NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id, created_at),
  UNIQUE (run_id, event_no, created_at)
) PARTITION BY RANGE (created_at);
CREATE TABLE run_events_default PARTITION OF run_events DEFAULT;
CREATE INDEX ix_run_events_resume ON run_events (tenant_id, run_id, event_no);

CREATE TABLE tool_calls (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  connection_id uuid REFERENCES provider_connections(id),
  step_id uuid REFERENCES run_steps(id),
  tool_name varchar(128) NOT NULL,
  risk_level varchar(16) NOT NULL CHECK (risk_level IN ('read','write','sensitive')),
  tool_version varchar(64) NOT NULL,
  mcp_server varchar(128),
  request_payload jsonb NOT NULL,
  input_summary jsonb NOT NULL,
  response_payload jsonb,
  output_summary jsonb,
  status varchar(32) NOT NULL CHECK (status IN ('pending','running','succeeded','failed','unknown')),
  error_code varchar(64), error_message varchar(1000),
  started_at timestamptz, finished_at timestamptz,
  duration_ms integer CHECK (duration_ms >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at)
);
CREATE INDEX ix_tool_calls_run ON tool_calls (tenant_id, run_id, created_at);
ALTER TABLE conversation_messages
  ADD CONSTRAINT fk_message_tool_call FOREIGN KEY (tool_call_id) REFERENCES tool_calls(id);

CREATE TABLE idempotency_records (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id), user_id uuid NOT NULL REFERENCES users(id),
  request_method varchar(8) NOT NULL, request_path varchar(512) NOT NULL,
  idempotency_key varchar(255) NOT NULL, request_hash char(64) NOT NULL,
  response_status smallint NOT NULL CHECK (response_status BETWEEN 100 AND 599),
  response_body jsonb NOT NULL, resource_type varchar(64), resource_id uuid,
  expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, user_id, request_method, request_path, idempotency_key),
  CHECK (expires_at > created_at)
);

CREATE TABLE calendar_event_drafts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
  user_id uuid NOT NULL REFERENCES users(id), run_id uuid NOT NULL REFERENCES agent_runs(id),
  connection_id uuid NOT NULL REFERENCES provider_connections(id),
  calendar_external_id varchar(255) NOT NULL, calendar_name varchar(256) NOT NULL,
  title varchar(500) NOT NULL, description text, start_at timestamptz NOT NULL,
  end_at timestamptz NOT NULL, timezone varchar(64) NOT NULL, version integer NOT NULL DEFAULT 1 CHECK (version > 0),
  status varchar(32) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','pending_approval','executed','invalidated')),
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (end_at > start_at)
);
CREATE INDEX ix_calendar_drafts_run ON calendar_event_drafts (tenant_id, user_id, run_id);

CREATE TABLE calendar_draft_attendees (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
  draft_id uuid NOT NULL REFERENCES calendar_event_drafts(id) ON DELETE CASCADE,
  display_name varchar(128), email varchar(320) NOT NULL, response_required boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (draft_id, email)
);

CREATE TABLE mail_sync_states (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
  user_id uuid NOT NULL REFERENCES users(id), connection_id uuid NOT NULL UNIQUE REFERENCES provider_connections(id),
  sync_cursor varchar(2048), status varchar(16) NOT NULL DEFAULT 'idle' CHECK (status IN ('idle','syncing','failed','disabled')),
  last_synced_at timestamptz, last_error_code varchar(64), last_error_message varchar(1000),
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_mail_sync_owner ON mail_sync_states (tenant_id, user_id, status);

CREATE TABLE email_messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
  user_id uuid NOT NULL REFERENCES users(id), connection_id uuid NOT NULL REFERENCES provider_connections(id),
  provider_message_id varchar(512) NOT NULL, provider_thread_id varchar(512),
  sender_name varchar(256), sender_email varchar(320) NOT NULL, subject varchar(998) NOT NULL,
  received_at timestamptz NOT NULL, source_version varchar(128) NOT NULL, content_hash char(64) NOT NULL,
  list_preview varchar(1000), snapshot_at timestamptz NOT NULL DEFAULT now(),
  extraction_status varchar(32) NOT NULL DEFAULT 'not_generated' CHECK (extraction_status IN ('not_generated','running','completed','failed','stale')),
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, user_id, connection_id, provider_message_id)
);
CREATE INDEX ix_email_messages_list ON email_messages (tenant_id, user_id, received_at DESC);

CREATE TABLE mail_extractions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
  user_id uuid NOT NULL REFERENCES users(id), email_id uuid NOT NULL REFERENCES email_messages(id),
  run_id uuid NOT NULL REFERENCES agent_runs(id), source_version varchar(128) NOT NULL, content_hash char(64) NOT NULL,
  extractor_version varchar(64) NOT NULL, model_provider varchar(64), model_name varchar(128), prompt_version varchar(64) NOT NULL,
  status varchar(32) NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed')),
  key_points jsonb, due_date_candidates jsonb, failure_reason varchar(1000), generated_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_mail_extractions_reuse ON mail_extractions (email_id, source_version, extractor_version, status);

CREATE TABLE mail_extraction_todos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
  extraction_id uuid NOT NULL REFERENCES mail_extractions(id) ON DELETE CASCADE,
  item_index integer NOT NULL CHECK (item_index >= 0), todo_text varchar(2000) NOT NULL,
  due_at timestamptz, due_text varchar(500), status varchar(32) NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate','drafted','discarded')),
  created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (extraction_id, item_index)
);

CREATE TABLE todo_drafts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id), user_id uuid NOT NULL REFERENCES users(id),
  email_id uuid NOT NULL REFERENCES email_messages(id), extraction_id uuid NOT NULL REFERENCES mail_extractions(id),
  extraction_todo_id uuid NOT NULL REFERENCES mail_extraction_todos(id), content varchar(2000) NOT NULL, due_at timestamptz,
  status varchar(32) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','discarded')),
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, extraction_todo_id)
);

CREATE TABLE mail_drafts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id), user_id uuid NOT NULL REFERENCES users(id),
  source_email_id uuid NOT NULL REFERENCES email_messages(id), source_extraction_id uuid REFERENCES mail_extractions(id),
  source_run_id uuid NOT NULL REFERENCES agent_runs(id), send_connection_id uuid NOT NULL REFERENCES provider_connections(id),
  subject varchar(998) NOT NULL, body text NOT NULL CHECK (length(body) <= 65536), version integer NOT NULL DEFAULT 1 CHECK (version > 0),
  status varchar(32) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','pending_approval','sent','invalidated','failed')),
  provider_message_id varchar(512), sent_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_mail_drafts_list ON mail_drafts (tenant_id, user_id, status, updated_at DESC);

CREATE TABLE mail_draft_recipients (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
  draft_id uuid NOT NULL REFERENCES mail_drafts(id) ON DELETE CASCADE,
  recipient_type varchar(8) NOT NULL CHECK (recipient_type IN ('to','cc','bcc')),
  display_name varchar(128), email varchar(320) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (draft_id, recipient_type, email)
);

CREATE TABLE approval_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id), user_id uuid NOT NULL REFERENCES users(id),
  run_id uuid NOT NULL REFERENCES agent_runs(id), action varchar(64) NOT NULL,
  risk_level varchar(16) NOT NULL CHECK (risk_level IN ('write','sensitive')),
  resource_type varchar(64) NOT NULL CHECK (resource_type IN ('calendar_event_draft','mail_draft')),
  resource_id uuid NOT NULL, title varchar(256) NOT NULL, preview_snapshot jsonb NOT NULL, parameters_hash char(64) NOT NULL,
  draft_version integer, status varchar(32) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved_executing','reconciling','executed','rejected','expired','failed','invalidated')),
  expires_at timestamptz NOT NULL, invalidated_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), CHECK (expires_at > created_at)
);
CREATE INDEX ix_approval_items_list ON approval_items (tenant_id, user_id, run_id, status);
CREATE INDEX ix_approval_items_expiry ON approval_items (status, expires_at) WHERE status = 'pending';

CREATE TABLE approval_decisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
  approval_item_id uuid NOT NULL UNIQUE REFERENCES approval_items(id), actor_user_id uuid NOT NULL REFERENCES users(id),
  decision varchar(16) NOT NULL CHECK (decision IN ('approved','rejected')), checked boolean NOT NULL,
  reason varchar(1000), approval_token_hash char(64), token_issued_at timestamptz, token_used_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((decision = 'approved' AND checked AND approval_token_hash IS NOT NULL) OR decision = 'rejected')
);

CREATE TABLE external_action_executions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
  approval_item_id uuid NOT NULL REFERENCES approval_items(id), connection_id uuid NOT NULL REFERENCES provider_connections(id),
  idempotency_key varchar(255) NOT NULL, status varchar(32) NOT NULL CHECK (status IN ('running','succeeded','failed','unknown')),
  provider_resource_id varchar(255), request_summary jsonb NOT NULL, result_summary jsonb,
  error_code varchar(64), error_message varchar(1000), attempt_no smallint NOT NULL DEFAULT 1 CHECK (attempt_no > 0),
  reconciliation_status varchar(32) NOT NULL DEFAULT 'not_required' CHECK (reconciliation_status IN ('not_required','pending','matched','not_found','failed')),
  next_reconcile_at timestamptz, reconciled_at timestamptz, started_at timestamptz, finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (approval_item_id, attempt_no), UNIQUE (connection_id, idempotency_key),
  CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE sent_mail_messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id), user_id uuid NOT NULL REFERENCES users(id),
  draft_id uuid NOT NULL UNIQUE REFERENCES mail_drafts(id), execution_id uuid NOT NULL UNIQUE REFERENCES external_action_executions(id),
  connection_id uuid NOT NULL REFERENCES provider_connections(id), provider_message_id varchar(512) NOT NULL,
  subject varchar(998) NOT NULL, recipient_summary jsonb NOT NULL, sent_at timestamptz NOT NULL,
  status varchar(32) NOT NULL DEFAULT 'sent' CHECK (status IN ('sent','failed')),
  created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (connection_id, provider_message_id)
);

CREATE TABLE memory_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id), user_id uuid NOT NULL REFERENCES users(id),
  content text NOT NULL CHECK (length(content) BETWEEN 1 AND 4000), source varchar(32) NOT NULL CHECK (source IN ('user_input','user_confirmed')),
  is_sensitive boolean NOT NULL DEFAULT false, deleted_at timestamptz, deleted_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((deleted_at IS NULL AND deleted_by IS NULL) OR (deleted_at IS NOT NULL AND deleted_by IS NOT NULL))
);
CREATE INDEX ix_memory_items_active ON memory_items (tenant_id, user_id, updated_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE memory_deletion_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
  memory_id uuid NOT NULL REFERENCES memory_items(id), requested_by uuid NOT NULL REFERENCES users(id),
  token_hash char(64) NOT NULL UNIQUE, status varchar(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active','used','expired','revoked')),
  expires_at timestamptz NOT NULL, used_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), CHECK (expires_at > created_at)
);

CREATE TABLE run_memory_usages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
  run_id uuid NOT NULL REFERENCES agent_runs(id), memory_id uuid NOT NULL REFERENCES memory_items(id),
  usage_purpose varchar(32) NOT NULL CHECK (usage_purpose IN ('context','recommendation')),
  included_in_prompt boolean NOT NULL, exclusion_reason varchar(256), display_summary varchar(1000),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((included_in_prompt AND exclusion_reason IS NULL) OR NOT included_in_prompt)
);

CREATE TABLE audit_events (
  id bigint GENERATED ALWAYS AS IDENTITY, tenant_id uuid NOT NULL REFERENCES tenants(id),
  actor_type varchar(32) NOT NULL CHECK (actor_type IN ('user','system','worker')), actor_id uuid,
  action varchar(128) NOT NULL, resource_type varchar(64) NOT NULL, resource_id uuid,
  run_id uuid REFERENCES agent_runs(id), approval_item_id uuid REFERENCES approval_items(id),
  request_id varchar(64), trace_id varchar(64), outcome varchar(16) NOT NULL CHECK (outcome IN ('succeeded','failed','denied','unknown')),
  detail jsonb NOT NULL, occurred_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);
CREATE TABLE audit_events_default PARTITION OF audit_events DEFAULT;
CREATE INDEX ix_audit_events_tenant_time ON audit_events (tenant_id, occurred_at DESC);
CREATE INDEX ix_audit_events_run ON audit_events (run_id) WHERE run_id IS NOT NULL;
CREATE INDEX ix_audit_events_approval ON audit_events (approval_item_id) WHERE approval_item_id IS NOT NULL;

CREATE TABLE outbox_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id), run_id uuid REFERENCES agent_runs(id),
  aggregate_type varchar(64) NOT NULL, aggregate_id uuid NOT NULL, event_type varchar(128) NOT NULL, payload jsonb NOT NULL,
  status varchar(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','published','failed')),
  retry_count smallint NOT NULL DEFAULT 0 CHECK (retry_count >= 0), available_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_outbox_dispatch ON outbox_events (status, available_at);
CREATE INDEX ix_outbox_run ON outbox_events (tenant_id, run_id, created_at);

-- 需要自动维护 updated_at 的表。
CREATE TRIGGER trg_tenants_updated BEFORE UPDATE ON tenants FOR EACH ROW EXECUTE FUNCTION aegis_set_updated_at();
CREATE TRIGGER trg_users_updated BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION aegis_set_updated_at();
CREATE TRIGGER trg_connections_updated BEFORE UPDATE ON provider_connections FOR EACH ROW EXECUTE FUNCTION aegis_set_updated_at();
CREATE TRIGGER trg_conversations_updated BEFORE UPDATE ON conversations FOR EACH ROW EXECUTE FUNCTION aegis_set_updated_at();
CREATE TRIGGER trg_runs_updated BEFORE UPDATE ON agent_runs FOR EACH ROW EXECUTE FUNCTION aegis_set_updated_at();
CREATE TRIGGER trg_calendar_drafts_updated BEFORE UPDATE ON calendar_event_drafts FOR EACH ROW EXECUTE FUNCTION aegis_set_updated_at();
CREATE TRIGGER trg_sync_states_updated BEFORE UPDATE ON mail_sync_states FOR EACH ROW EXECUTE FUNCTION aegis_set_updated_at();
CREATE TRIGGER trg_email_messages_updated BEFORE UPDATE ON email_messages FOR EACH ROW EXECUTE FUNCTION aegis_set_updated_at();
CREATE TRIGGER trg_todo_drafts_updated BEFORE UPDATE ON todo_drafts FOR EACH ROW EXECUTE FUNCTION aegis_set_updated_at();
CREATE TRIGGER trg_mail_drafts_updated BEFORE UPDATE ON mail_drafts FOR EACH ROW EXECUTE FUNCTION aegis_set_updated_at();
CREATE TRIGGER trg_approval_items_updated BEFORE UPDATE ON approval_items FOR EACH ROW EXECUTE FUNCTION aegis_set_updated_at();
CREATE TRIGGER trg_memory_items_updated BEFORE UPDATE ON memory_items FOR EACH ROW EXECUTE FUNCTION aegis_set_updated_at();

-- RLS：应用请求必须通过 SET LOCAL 注入认证上下文；Worker 使用独立受控角色。
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE mail_drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE sent_mail_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE todo_drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE mail_sync_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE approval_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_deletion_tokens ENABLE ROW LEVEL SECURITY;
CREATE POLICY conversations_owner ON conversations USING (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id()) WITH CHECK (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id());
CREATE POLICY connections_owner ON provider_connections USING (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id()) WITH CHECK (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id());
CREATE POLICY email_messages_owner ON email_messages USING (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id()) WITH CHECK (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id());
CREATE POLICY mail_drafts_owner ON mail_drafts USING (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id()) WITH CHECK (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id());
CREATE POLICY sent_mail_owner ON sent_mail_messages USING (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id()) WITH CHECK (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id());
CREATE POLICY todo_drafts_owner ON todo_drafts USING (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id()) WITH CHECK (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id());
CREATE POLICY mail_sync_owner ON mail_sync_states USING (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id()) WITH CHECK (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id());
CREATE POLICY approval_items_owner ON approval_items USING (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id()) WITH CHECK (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id());
CREATE POLICY memory_items_owner ON memory_items USING (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id()) WITH CHECK (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id());
CREATE POLICY memory_deletion_tokens_owner ON memory_deletion_tokens USING (tenant_id = aegis_current_tenant_id() AND requested_by = aegis_current_user_id()) WITH CHECK (tenant_id = aegis_current_tenant_id() AND requested_by = aegis_current_user_id());

COMMIT;
