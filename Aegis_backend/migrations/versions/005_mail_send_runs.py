"""支持受确认控制的邮件发送任务。"""

from alembic import op


revision = "005_mail_send_runs"
down_revision = "004_todo_plan_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为 agent_runs 新增 mail_send 任务类型。"""
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_run_type_check")
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_run_type_check "
        "CHECK (run_type IN ('conversation', 'mail_extraction', 'mail_reply_draft', 'mail_send'))"
    )


def downgrade() -> None:
    """回退时拒绝存在 mail_send 数据，避免静默丢失任务语义。"""
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_run_type_check")
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_run_type_check "
        "CHECK (run_type IN ('conversation', 'mail_extraction', 'mail_reply_draft'))"
    )
