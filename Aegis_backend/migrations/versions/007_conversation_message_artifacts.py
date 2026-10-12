"""保存 Agent 生成的短期对话卡片与业务对象引用。"""

from alembic import op


revision = "007_conversation_artifacts"
down_revision = "006_memory_source_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建租户隔离的对话 Artifact 表及其 RLS 策略。"""
    op.execute(
        "CREATE TABLE conversation_message_artifacts ("
        "  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
        "  tenant_id uuid NOT NULL REFERENCES tenants(id),"
        "  user_id uuid NOT NULL REFERENCES users(id),"
        "  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,"
        "  assistant_message_id uuid REFERENCES conversation_messages(id) ON DELETE SET NULL,"
        "  run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,"
        "  artifact_type varchar(64) NOT NULL,"
        "  payload jsonb NOT NULL,"
        "  expires_at timestamptz NOT NULL,"
        "  created_at timestamptz NOT NULL DEFAULT now(),"
        "  UNIQUE (run_id, artifact_type),"
        "  CHECK (expires_at > created_at)"
        ")"
    )
    op.execute(
        "CREATE INDEX ix_conversation_artifacts_lookup "
        "ON conversation_message_artifacts "
        "(tenant_id, user_id, conversation_id, created_at DESC)"
    )
    op.execute("ALTER TABLE conversation_message_artifacts ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY conversation_artifacts_owner ON conversation_message_artifacts "
        "USING (tenant_id = aegis_current_tenant_id() "
        "       AND user_id = aegis_current_user_id()) "
        "WITH CHECK (tenant_id = aegis_current_tenant_id() "
        "            AND user_id = aegis_current_user_id())"
    )


def downgrade() -> None:
    """删除短期 Artifact 表及其关联索引。"""
    op.execute("DROP TABLE conversation_message_artifacts")
