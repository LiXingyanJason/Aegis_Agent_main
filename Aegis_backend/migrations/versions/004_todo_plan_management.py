"""为 Aegis 站内待办计划增加完成状态。"""

from alembic import op


revision = "004_todo_plan_management"
down_revision = "003_mail_background_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """支持待办完成时间和 completed 状态，供待办管理页面使用。"""
    op.execute("ALTER TABLE todo_drafts ADD COLUMN completed_at timestamptz")
    op.execute("ALTER TABLE todo_drafts DROP CONSTRAINT todo_drafts_status_check")
    op.execute(
        "ALTER TABLE todo_drafts ADD CONSTRAINT todo_drafts_status_check "
        "CHECK (status IN ('draft', 'completed', 'discarded'))"
    )
    op.execute(
        "CREATE INDEX ix_todo_drafts_plan "
        "ON todo_drafts (tenant_id, user_id, status, due_at ASC NULLS LAST, updated_at DESC)"
    )


def downgrade() -> None:
    """拒绝自动回滚，以免丢失用户标记的完成时间与状态。"""
    raise RuntimeError(
        "004_todo_plan_management 包含用户维护的完成状态；"
        "请在隔离环境归档数据后再执行人工回滚。"
    )
