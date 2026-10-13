"""任务预算、用户补充请求和草稿修改任务。"""
from alembic import op

revision = "008_agent_harness"
down_revision = "007_conversation_artifacts"
branch_labels = depends_on = None


def upgrade():
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_run_type_check")
    op.execute("ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_run_type_check CHECK (run_type IN ('conversation','mail_extraction','mail_reply_draft','mail_send','mail_draft_revision'))")
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_status_check")
    op.execute("ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check CHECK (status IN ('queued','running','waiting_input','waiting_confirmation','completed','failed','cancelled'))")
    ddl="""
      CREATE TABLE run_execution_budgets (
        run_id uuid PRIMARY KEY REFERENCES agent_runs(id) ON DELETE CASCADE,
        tenant_id uuid NOT NULL REFERENCES tenants(id), user_id uuid NOT NULL REFERENCES users(id),
        policy_version varchar(64) NOT NULL, model_calls integer NOT NULL DEFAULT 0 CHECK(model_calls >= 0),
        tool_calls integer NOT NULL DEFAULT 0 CHECK(tool_calls >= 0),
        active_ms bigint NOT NULL DEFAULT 0 CHECK(active_ms >= 0)
      );
      CREATE TABLE run_execution_attempts (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), run_id uuid NOT NULL REFERENCES agent_runs(id),
        tenant_id uuid NOT NULL REFERENCES tenants(id), user_id uuid NOT NULL REFERENCES users(id),
        kind varchar(16) NOT NULL CHECK(kind IN ('model','tool')),
        label varchar(128) NOT NULL, status varchar(16) NOT NULL DEFAULT 'running' CHECK(status IN ('running','succeeded','failed')),
        risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
        error_code varchar(64), started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz
      );
      CREATE TABLE run_input_requests (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
        user_id uuid NOT NULL REFERENCES users(id), run_id uuid NOT NULL REFERENCES agent_runs(id),
        question varchar(500) NOT NULL, version integer NOT NULL DEFAULT 1 CHECK(version>0),
        status varchar(16) NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','answered','consumed','cancelled','expired')),
        ready boolean NOT NULL DEFAULT false, response varchar(2000), client_response_id varchar(128),
        expires_at timestamptz NOT NULL DEFAULT now() + interval '24 hours',
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(run_id, version)
      );
      CREATE TABLE mail_draft_revision_requests (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
        user_id uuid NOT NULL REFERENCES users(id), run_id uuid NOT NULL UNIQUE REFERENCES agent_runs(id),
        draft_id uuid NOT NULL REFERENCES mail_drafts(id), expected_version integer NOT NULL CHECK(expected_version>0),
        instruction varchar(2000) NOT NULL, client_request_id varchar(128) NOT NULL,
        status varchar(16) NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','succeeded','failed')), created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(tenant_id,user_id,draft_id,client_request_id)
      );
      CREATE INDEX ix_input_requests_owner ON run_input_requests(tenant_id,user_id,run_id,created_at);
    """
    for statement in ddl.split(";"):
        if statement.strip():
            op.execute(statement)
    for table in ("run_execution_budgets","run_execution_attempts","run_input_requests","mail_draft_revision_requests"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_owner ON {table} USING (tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id()) WITH CHECK (tenant_id=aegis_current_tenant_id() AND user_id=aegis_current_user_id())")


def downgrade():
    # 有新类型或暂停任务时约束回退会失败，避免默默丢弃任务状态。
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_status_check")
    op.execute("ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check CHECK(status IN ('queued','running','waiting_confirmation','completed','failed','cancelled'))")
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_run_type_check")
    op.execute("ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_run_type_check CHECK(run_type IN ('conversation','mail_extraction','mail_reply_draft','mail_send'))")
    for table in ("mail_draft_revision_requests","run_input_requests","run_execution_attempts","run_execution_budgets"):
        op.execute(f"DROP TABLE {table}")
