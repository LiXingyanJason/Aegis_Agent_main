"""支持不归属聊天会话的邮件后台任务。"""

from alembic import op


revision = "003_mail_background_runs"
down_revision = "002_msg_client_id_uq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """允许邮件提取/起草任务没有 conversation，并保存起草请求上下文。"""
    op.execute("ALTER TABLE agent_runs ALTER COLUMN conversation_id DROP NOT NULL")
    op.execute(
        "ALTER TABLE mail_extraction_todos "
        "ADD COLUMN is_inferred boolean NOT NULL DEFAULT true"
    )
    op.execute(
        """
        CREATE TABLE mail_reply_draft_requests (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          user_id uuid NOT NULL REFERENCES users(id),
          email_id uuid NOT NULL REFERENCES email_messages(id),
          extraction_id uuid REFERENCES mail_extractions(id),
          run_id uuid NOT NULL UNIQUE REFERENCES agent_runs(id) ON DELETE CASCADE,
          instruction varchar(2000),
          status varchar(32) NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued','running','succeeded','failed')),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_mail_reply_draft_requests_owner "
        "ON mail_reply_draft_requests (tenant_id, user_id, created_at DESC)"
    )
    op.execute(
        "CREATE TRIGGER trg_mail_reply_draft_requests_updated BEFORE UPDATE "
        "ON mail_reply_draft_requests FOR EACH ROW EXECUTE FUNCTION aegis_set_updated_at()"
    )
    # 与既有业务表一致：API 设置的 app.tenant_id / app.user_id 会限制行可见性。
    op.execute("ALTER TABLE mail_reply_draft_requests ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY mail_reply_draft_requests_owner ON mail_reply_draft_requests "
        "USING (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id()) "
        "WITH CHECK (tenant_id = aegis_current_tenant_id() AND user_id = aegis_current_user_id())"
    )


def downgrade() -> None:
    """拒绝自动回滚：邮件运行可能已被草稿、待办和审计记录引用。"""
    raise RuntimeError(
        "003_mail_background_runs 包含可能已被业务记录引用的邮件任务；"
        "请在隔离环境中完成数据归档与人工清理后再执行回滚。"
    )
