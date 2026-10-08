"""记录用户确认记忆的来源任务。"""

from alembic import op


revision = "006_memory_source_run"
down_revision = "005_mail_send_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为 user_confirmed 记忆保留可追溯的来源任务。"""
    op.execute(
        "ALTER TABLE memory_items ADD COLUMN source_run_id uuid "
        "REFERENCES agent_runs(id)"
    )
    op.execute(
        "CREATE INDEX ix_memory_items_source_run "
        "ON memory_items (tenant_id, user_id, source_run_id) "
        "WHERE source_run_id IS NOT NULL"
    )


def downgrade() -> None:
    """移除确认来源关联字段及索引。"""
    op.execute("DROP INDEX IF EXISTS ix_memory_items_source_run")
    op.execute("ALTER TABLE memory_items DROP COLUMN source_run_id")
